from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sqlalchemy import create_engine, select

from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import Run, RunConfigurationSnapshot, Task
from agentic_os.sandbox import runtime_available

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
    global TEST_DATABASE_URL, client
    TEST_DATABASE_URL = os.environ.get("AGENTIC_OS_DATABASE_URL", database_url())
    try:
        probe = create_database_engine(TEST_DATABASE_URL)
        with probe.connect():
            pass
        probe.dispose()
    except Exception as error:  # pragma: no cover - environment guard
        raise unittest.SkipTest(
            f"PostgreSQL is not reachable at {TEST_DATABASE_URL!r}; set "
            "AGENTIC_OS_DATABASE_URL to run restart-recovery verification: "
            f"{error}"
        )
    os.environ["AGENTIC_OS_DATABASE_URL"] = TEST_DATABASE_URL
    _apply_migrations_from_zero(TEST_DATABASE_URL)

    from fastapi.testclient import TestClient

    from agentic_os.api import deps as api_deps
    from agentic_os.api.app import create_app

    # The API layer caches a single process-wide engine (agentic_os.api.deps
    # ._engine). If an earlier test module already warmed that cache against
    # the pre-reset schema, its pooled connections hold server-side prepared
    # statements referencing table OIDs this module just dropped and
    # recreated above; clear it so requests below build fresh connections.
    api_deps._engine.cache_clear()

    client = TestClient(create_app())


class RestartRecoveryTests(unittest.TestCase):
    """Exit criterion 6: a deliberate mid-run process kill and restart of the
    worker resumes from the last committed PostgreSQL boundary, without
    losing acknowledged work or duplicating a completed step, and all
    progress/audit/cost/artifact evidence remains inspectable afterward.
    """

    def setUp(self) -> None:
        self.engine = create_database_engine(TEST_DATABASE_URL)
        self.Session = session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _run_worker_subprocess(
        self, *, worker_id: str, lease_seconds: int, pause_seconds: float | None = None
    ) -> subprocess.Popen:
        env = dict(os.environ, AGENTIC_OS_DATABASE_URL=TEST_DATABASE_URL, PYTHONPATH=str(BACKEND_ROOT / "src"))
        if pause_seconds is not None:
            env["AGENTIC_OS_WORKER_TEST_PAUSE_AFTER_RUN_STARTED_SECONDS"] = str(pause_seconds)
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agentic_os",
                "worker",
                "run-once",
                "--worker-id",
                worker_id,
                "--lease-seconds",
                str(lease_seconds),
            ],
            cwd=BACKEND_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _wait_for_run_status(self, task_id: uuid.UUID, status: str, *, timeout: float = 15.0) -> Run:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.Session() as session:
                run = session.execute(
                    select(Run).where(Run.task_id == task_id).order_by(Run.attempt_number.desc()).limit(1)
                ).scalar_one_or_none()
                if run is not None and run.status == status:
                    return run
            time.sleep(0.2)
        self.fail(f"run for task {task_id} did not reach status {status!r} within {timeout}s")

    def _configure_workflow_task(
        self, *, sandbox: dict | None
    ) -> tuple[uuid.UUID, uuid.UUID, dict[str, str]]:
        """Configures model profile, project, goal, skill, MCP tool, agent, and
        budget through the versioned API, then persists a ready task directly
        (there is no task-creation endpoint yet). Returns (project_id, task_id).
        """
        model_secret = "sk-restart-verification-secret"
        model_profile_response = client.post(
            "/api/v1/model-profiles",
            json={
                "name": "restart-verify-profile",
                "base_url": "https://api.example.com/v1",
                "model_identifier": "gpt-test",
                "api_key": model_secret,
            },
        )
        model_profile_response.raise_for_status()
        self.assertNotIn(model_secret, model_profile_response.text)
        model_profile = model_profile_response.json()
        profile_versions_response = client.get(
            f"/api/v1/model-profiles/{model_profile['id']}/versions"
        )
        profile_versions_response.raise_for_status()
        self.assertNotIn(model_secret, profile_versions_response.text)
        model_profile_version = profile_versions_response.json()[0]

        project = client.post("/api/v1/projects", json={"name": "Restart Recovery Project"}).json()
        goal = client.post(
            f"/api/v1/projects/{project['id']}/goals", json={"title": "Survive a restart"}
        ).json()

        skill = client.post("/api/v1/skills", json={"name": "Restart Verification Skill"}).json()
        skill_version = client.post(
            f"/api/v1/skills/{skill['id']}/versions",
            json={"content_ref": "skills/restart-verify/v1", "resource_metadata": {}},
        ).json()

        mcp_server = client.post("/api/v1/mcp-servers", json={"name": "Restart Verification MCP Server"}).json()
        mcp_version = client.post(
            f"/api/v1/mcp-servers/{mcp_server['id']}/versions",
            json={
                "connection_config": {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo input",
                            "pricing": {
                                "chargeable": True,
                                "amount_minor_units": 25,
                                "currency": "USD",
                            },
                        }
                    ]
                }
            },
        ).json()

        policy_set = client.post(
            "/api/v1/policy-sets", json={"name": "Restart Verification Policy"}
        ).json()
        policy_version = client.post(
            f"/api/v1/policy-sets/{policy_set['id']}/versions",
            json={"rules": [{"action": "mcp_tool_call", "decision": "allow"}]},
        ).json()

        agent = client.post("/api/v1/agents", json={"name": "Restart Verification Agent"}).json()
        budget = client.post(
            f"/api/v1/agents/{agent['id']}/budgets",
            json={"currency": "USD", "amount_minor_units": 10_00, "enforcement_mode": "hard_stop"},
        ).json()

        capability_manifest = {
            "skill_version_id": skill_version["id"],
            "mcp_server_version_id": mcp_version["id"],
            "enabled_tools": ["echo"],
        }
        if sandbox is not None:
            capability_manifest["sandbox"] = sandbox

        agent_version = client.post(
            f"/api/v1/agents/{agent['id']}/versions",
            json={
                "instructions": "Use the echo tool.",
                "capability_manifest": capability_manifest,
                "model_profile_id": model_profile["id"],
                "default_budget_id": budget["id"],
                "skill_attachments": [{"version_id": skill_version["id"], "config": {}}],
                "mcp_server_attachments": [{"version_id": mcp_version["id"], "config": {}}],
                "policy_set_version_ids": [policy_version["id"]],
            },
        )
        agent_version.raise_for_status()
        agent_version = agent_version.json()

        with self.Session() as session:
            task = Task(
                goal_id=uuid.UUID(goal["id"]),
                title="Governed restart-recovery task",
                status="pending",
                assigned_agent_version_id=uuid.UUID(agent_version["id"]),
            )
            session.add(task)
            session.commit()
            task_id = task.id

        return (
            uuid.UUID(project["id"]),
            task_id,
            {
                "model_secret": model_secret,
                "model_profile_version_id": model_profile_version["id"],
                "skill_version_id": skill_version["id"],
                "mcp_server_version_id": mcp_version["id"],
                "policy_set_version_id": policy_version["id"],
                "budget_id": budget["id"],
                "agent_version_id": agent_version["id"],
            },
        )

    def test_worker_process_kill_and_restart_resumes_without_duplicating_work(self) -> None:
        sandbox_config = None
        available, _reason = runtime_available("docker")
        if not available:
            available, _reason = runtime_available("podman")
        if available:
            sandbox_config = {
                "image": "alpine:latest",
                "command": ["true"],
                "network_policy": "none",
                "cpu_limit": 1.0,
                "memory_limit_mb": 256,
                "timeout_seconds": 30,
            }

        project_id, task_id, expected = self._configure_workflow_task(sandbox=sandbox_config)

        with self.Session() as session:
            task = session.get(Task, task_id)
            self.assertEqual(task.status, "pending")

        # Start the worker as a real OS process with a short lease and a
        # controlled pause right after the run is durably committed as
        # "running" -- this is the point the harness kills the process.
        first_process = self._run_worker_subprocess(worker_id="restart-verify-1", lease_seconds=3, pause_seconds=20)
        try:
            first_attempt = self._wait_for_run_status(task_id, "running", timeout=15.0)
            self.assertEqual(first_attempt.attempt_number, 1)

            with self.Session() as session:
                task = session.get(Task, task_id)
                self.assertEqual(task.status, "running")
                self.assertEqual(task.lease_owner, "restart-verify-1")

            # Deliberate mid-run process termination.
            first_process.kill()
            first_process.wait(timeout=10)
        finally:
            if first_process.poll() is None:
                first_process.kill()
                first_process.wait(timeout=10)
            first_process.stdout.close()
            first_process.stderr.close()

        # Confirm the acknowledged run state persisted across the kill: it is
        # still "running" in PostgreSQL, an explicit, inspectable recoverable
        # state, even though the process that owned it is gone.
        with self.Session() as session:
            stuck_run = session.get(Run, first_attempt.id)
            self.assertEqual(stuck_run.status, "running")
            task = session.get(Task, task_id)
            self.assertEqual(task.status, "running")

        # Let the short lease expire, then restart with a fresh worker
        # process -- the equivalent of an operator bringing the worker back
        # up after a crash.
        time.sleep(4)

        second_process = self._run_worker_subprocess(worker_id="restart-verify-2", lease_seconds=30)
        stdout, stderr = second_process.communicate(timeout=30)
        self.assertEqual(second_process.returncode, 0, stderr)

        with self.Session() as session:
            task = session.get(Task, task_id)
            self.assertEqual(task.status, "completed")
            self.assertIsNone(task.lease_owner)
            self.assertIsNone(task.lease_expires_at)

            runs = list(
                session.execute(select(Run).where(Run.task_id == task_id).order_by(Run.attempt_number)).scalars()
            )
            self.assertEqual(len(runs), 2)
            self.assertEqual(runs[0].id, first_attempt.id)
            self.assertEqual(runs[0].status, "failed")
            self.assertEqual(runs[1].status, "completed")
            self.assertEqual(runs[1].attempt_number, 2)
            self.assertEqual(runs[1].idempotency_key, f"{task_id}:2")
            self.assertEqual(
                runs[0].snapshot["configuration_snapshot_id"],
                runs[1].snapshot["configuration_snapshot_id"],
            )
            self.assertIsNotNone(runs[0].snapshot["model_profile_version_id"])
            self.assertEqual(
                runs[0].snapshot["model_profile_version_id"],
                runs[1].snapshot["model_profile_version_id"],
            )
            self.assertEqual(
                runs[1].snapshot["model_profile_version_id"],
                expected["model_profile_version_id"],
            )
            self.assertEqual(runs[1].snapshot["skill_version_ids"], [expected["skill_version_id"]])
            self.assertEqual(
                runs[1].snapshot["mcp_server_version_ids"],
                [expected["mcp_server_version_id"]],
            )
            self.assertEqual(runs[1].snapshot["default_budget_id"], expected["budget_id"])
            self.assertEqual(runs[1].snapshot["agent_version_id"], expected["agent_version_id"])
            self.assertEqual(runs[1].snapshot["enabled_tools"], ["echo"])
            self.assertEqual(runs[1].snapshot["policy_decision"], "allow")
            snapshots = list(
                session.execute(
                    select(RunConfigurationSnapshot)
                    .join(Run, RunConfigurationSnapshot.run_id == Run.id)
                    .where(Run.task_id == task_id)
                ).scalars()
            )
            self.assertEqual(len(snapshots), 1)
            snapshot_configuration = snapshots[0].configuration
            self.assertEqual(
                [item["id"] for item in snapshot_configuration["policy_sets"]],
                [expected["policy_set_version_id"]],
            )
            self.assertNotIn(expected["model_secret"], str(snapshot_configuration))
            completed_run_id = runs[1].id

        # Progress/audit/cost/artifact evidence remains inspectable through
        # the API after the restart, not just via direct DB access.
        api_runs = client.get(f"/api/v1/tasks/{task_id}/runs").json()
        self.assertEqual(len(api_runs), 2)
        self.assertEqual(api_runs[0]["status"], "failed")
        self.assertEqual(api_runs[1]["status"], "completed")
        self.assertEqual(
            api_runs[0]["snapshot"]["configuration_snapshot_id"],
            api_runs[1]["snapshot"]["configuration_snapshot_id"],
        )
        self.assertNotIn(expected["model_secret"], str(api_runs))

        audit_events = client.get("/api/v1/audit-events", params={"task_id": str(task_id)}).json()
        event_types = [event["event_type"] for event in audit_events]
        self.assertIn("run.interrupted", event_types)
        self.assertIn("run.completed", event_types)
        self.assertIn("policy.decision", event_types)
        self.assertIn("tool.invoked", event_types)
        interrupted_event = next(event for event in audit_events if event["event_type"] == "run.interrupted")
        self.assertEqual(interrupted_event["run_id"], str(first_attempt.id))

        artifacts = client.get(
            "/api/v1/projects/{}/artifacts".format(project_id), params={"task_id": str(task_id)}
        ).json()
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["run_id"], str(completed_run_id))

        ledger_entries = client.get("/api/v1/cost-ledger-entries", params={"run_id": str(completed_run_id)}).json()
        self.assertEqual(len(ledger_entries), 1)
        self.assertEqual(ledger_entries[0]["budget_id"], expected["budget_id"])
        self.assertEqual(ledger_entries[0]["reserved_amount_minor_units"], 25)
        self.assertEqual(ledger_entries[0]["actual_amount_minor_units"], 25)
        self.assertEqual(ledger_entries[0]["status"], "reconciled")
        self.assertFalse(ledger_entries[0]["is_zero_cost"])

        # Duplicate execution is prevented: the completed task is no longer
        # claimable, so a further worker restart finds nothing to do.
        third_process = self._run_worker_subprocess(worker_id="restart-verify-3", lease_seconds=30)
        stdout3, stderr3 = third_process.communicate(timeout=30)
        self.assertEqual(third_process.returncode, 0, stderr3)
        self.assertIn("claimed and processed 0 task(s)", stdout3)

        with self.Session() as session:
            runs = list(session.execute(select(Run).where(Run.task_id == task_id)).scalars())
            self.assertEqual(len(runs), 2)


if __name__ == "__main__":
    unittest.main()
