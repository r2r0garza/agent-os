from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sqlalchemy import create_engine

from agentic_os.domain import create_database_engine, database_url

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
            "AGENTIC_OS_DATABASE_URL to run API tests: "
            f"{error}"
        )
    os.environ["AGENTIC_OS_DATABASE_URL"] = TEST_DATABASE_URL
    _apply_migrations_from_zero(TEST_DATABASE_URL)

    from fastapi.testclient import TestClient

    from agentic_os.api.app import create_app

    client = TestClient(create_app())


class ApiWorkflowTests(unittest.TestCase):
    """Proves the versioned API is backed by persisted PostgreSQL records
    (exit criterion 2), not mock-only behavior."""

    def test_configure_model_profile_redacts_secret(self) -> None:
        response = client.post(
            "/api/v1/model-profiles",
            json={
                "name": "primary",
                "base_url": "https://api.example.com/v1",
                "model_identifier": "gpt-test",
                "api_key": "sk-super-secret",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertNotIn("api_key", body)
        self.assertNotIn("ciphertext", str(body))
        self.assertIn("id", body)

    def test_model_profile_validation_failure(self) -> None:
        response = client.post("/api/v1/model-profiles", json={"name": "incomplete"})
        self.assertEqual(response.status_code, 422)

    def test_not_found_returns_404(self) -> None:
        response = client.get(f"/api/v1/projects/{uuid.uuid4()}")
        self.assertEqual(response.status_code, 404)

    def test_malformed_id_returns_422(self) -> None:
        response = client.get("/api/v1/projects/not-a-uuid")
        self.assertEqual(response.status_code, 422)

    def test_openapi_schema_exposes_versioned_routes(self) -> None:
        schema = client.get("/openapi.json").json()
        for path in ("/api/v1/projects", "/api/v1/model-profiles", "/api/v1/agents", "/api/v1/skills"):
            self.assertIn(path, schema["paths"])

    def test_project_goal_agent_skill_mcp_budget_workflow(self) -> None:
        project = client.post("/api/v1/projects", json={"name": "Demo Project"}).json()

        goal = client.post(f"/api/v1/projects/{project['id']}/goals", json={"title": "Ship it"}).json()
        self.assertEqual(goal["status"], "draft")

        skill = client.post("/api/v1/skills", json={"name": "Research"}).json()
        skill_version = client.post(
            f"/api/v1/skills/{skill['id']}/versions",
            json={"content_ref": "s3://skills/research/v1", "resource_metadata": {}},
        ).json()
        self.assertEqual(skill_version["version_number"], 1)

        mcp_server = client.post("/api/v1/mcp-servers", json={"name": "Test Tool Server"}).json()
        mcp_version = client.post(
            f"/api/v1/mcp-servers/{mcp_server['id']}/versions",
            json={
                "connection_config": {"tools": [{"name": "echo", "description": "Echo input"}]},
                "credential": "shhh",
            },
        ).json()
        self.assertTrue(mcp_version["credential_configured"])
        self.assertNotIn("credential", mcp_version)
        self.assertNotIn("ciphertext", str(mcp_version))

        tools = client.get(
            f"/api/v1/mcp-servers/{mcp_server['id']}/versions/{mcp_version['version_number']}/tools"
        ).json()
        self.assertEqual(tools, [{"name": "echo", "description": "Echo input"}])

        agent = client.post("/api/v1/agents", json={"name": "Operator Agent"}).json()
        agent_version = client.post(
            f"/api/v1/agents/{agent['id']}/versions",
            json={
                "instructions": "Use the research skill and echo tool.",
                "capability_manifest": {
                    "skill_version_id": skill_version["id"],
                    "mcp_server_version_id": mcp_version["id"],
                    "enabled_tools": ["echo"],
                },
            },
        ).json()
        self.assertEqual(agent_version["version_number"], 1)

        budget = client.post(
            f"/api/v1/agents/{agent['id']}/budgets",
            json={"currency": "USD", "amount_minor_units": 500000, "enforcement_mode": "hard_stop"},
        ).json()
        self.assertEqual(budget["enforcement_mode"], "hard_stop")
        self.assertEqual(budget["agent_id"], agent["id"])

        tasks = client.get(f"/api/v1/goals/{goal['id']}/tasks").json()
        self.assertEqual(tasks, [])

        audit_events = client.get("/api/v1/audit-events", params={"project_id": project["id"]}).json()
        self.assertEqual(audit_events, [])

    def test_state_inspection_reads_persisted_records(self) -> None:
        from agentic_os.domain import create_database_engine, session_factory
        from agentic_os.domain.models import AuditEvent, Task

        project = client.post("/api/v1/projects", json={"name": "Inspect Project"}).json()
        goal = client.post(f"/api/v1/projects/{project['id']}/goals", json={"title": "Inspect goal"}).json()

        engine = create_database_engine(TEST_DATABASE_URL)
        session_maker = session_factory(engine)
        with session_maker() as session:
            task = Task(goal_id=uuid.UUID(goal["id"]), title="Persisted task", required_capabilities={})
            session.add(task)
            session.flush()
            session.add(
                AuditEvent(
                    project_id=uuid.UUID(project["id"]),
                    event_type="task.created",
                    payload={"task_id": str(task.id)},
                )
            )
            session.commit()
            task_id = task.id
        engine.dispose()

        tasks = client.get(f"/api/v1/goals/{goal['id']}/tasks").json()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], str(task_id))

        fetched_task = client.get(f"/api/v1/tasks/{task_id}").json()
        self.assertEqual(fetched_task["title"], "Persisted task")

        events = client.get("/api/v1/audit-events", params={"project_id": project["id"]}).json()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "task.created")


if __name__ == "__main__":
    unittest.main()
