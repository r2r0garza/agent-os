from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sqlalchemy import create_engine, select

from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import ObservabilityRecord, TelemetryExportAttempt

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

    def test_request_context_propagates_to_goal_observability(self) -> None:
        request_id = uuid.uuid4()
        headers = {"x-request-id": str(request_id)}
        project_response = client.post(
            "/api/v1/projects",
            json={"name": f"Correlated Project {uuid.uuid4()}"},
            headers=headers,
        )
        self.assertEqual(project_response.status_code, 201, project_response.text)
        project = project_response.json()
        goal_response = client.post(
            f"/api/v1/projects/{project['id']}/goals",
            json={"title": "Correlated goal"},
            headers=headers,
        )
        self.assertEqual(goal_response.status_code, 201, goal_response.text)
        self.assertEqual(goal_response.headers["x-request-id"], str(request_id))
        self.assertEqual(goal_response.headers["x-correlation-id"], str(request_id))
        self.assertTrue(goal_response.headers["traceparent"].startswith("00-"))

        engine = create_database_engine(TEST_DATABASE_URL)
        Session = session_factory(engine)
        with Session() as session:
            records = list(
                session.execute(
                    select(ObservabilityRecord).where(
                        ObservabilityRecord.correlation_id == request_id
                    )
                ).scalars()
            )
            self.assertTrue({"request", "goal"} <= {record.event_kind for record in records})
            goal_record = next(record for record in records if record.event_kind == "goal")
            self.assertEqual(str(goal_record.goal_id), goal_response.json()["id"])
            self.assertIn(goal_record.trace_id, goal_response.headers["traceparent"])
            attempts = list(
                session.execute(
                    select(TelemetryExportAttempt).where(
                        TelemetryExportAttempt.observability_record_id.in_(
                            [record.id for record in records]
                        )
                    )
                ).scalars()
            )
            self.assertEqual({attempt.status for attempt in attempts}, {"disabled"})
        engine.dispose()

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

    def test_governed_configuration_versions_and_redacts_sensitive_values(self) -> None:
        secret = "credential-value-that-must-never-return"
        credential_response = client.post(
            "/api/v1/credentials",
            json={
                "name": "Provider credential",
                "credential_type": "api_key",
                "material": secret,
                "metadata": {"provider": "compatible"},
            },
        )
        self.assertEqual(credential_response.status_code, 201, credential_response.text)
        credential = credential_response.json()
        self.assertTrue(credential["configured"])
        self.assertNotIn(secret, credential_response.text)
        self.assertNotIn("encrypted_material", credential_response.text)
        self.assertEqual(client.get(f"/api/v1/credentials/{credential['id']}").json(), credential)
        self.assertIn(credential, client.get("/api/v1/credentials").json())
        immutable = client.patch(f"/api/v1/credentials/{credential['id']}", json={"material": "replacement"})
        self.assertEqual(immutable.status_code, 409)
        self.assertNotIn("replacement", immutable.text)

        profile = client.post(
            "/api/v1/model-profiles",
            json={
                "name": "Governed profile",
                "base_url": "https://models.example.test/v1",
                "model_identifier": "model-v1",
                "credential_id": credential["id"],
            },
        ).json()
        profile_versions = client.get(f"/api/v1/model-profiles/{profile['id']}/versions").json()
        self.assertEqual([item["version_number"] for item in profile_versions], [1])
        header_secret = "Bearer header-secret"
        profile_v2_response = client.post(
            f"/api/v1/model-profiles/{profile['id']}/versions",
            json={
                "base_url": "https://models.example.test/v2",
                "model_identifier": "model-v2",
                "credential_id": credential["id"],
                "headers": {"Authorization": header_secret, "X-Region": "local"},
            },
        )
        self.assertEqual(profile_v2_response.status_code, 201, profile_v2_response.text)
        profile_v2 = profile_v2_response.json()
        self.assertEqual(profile_v2["version_number"], 2)
        self.assertEqual(profile_v2["headers"]["Authorization"], "[REDACTED]")
        self.assertNotIn(header_secret, profile_v2_response.text)
        self.assertEqual(
            client.get(f"/api/v1/model-profiles/{profile['id']}/versions/1").json()["model_identifier"],
            "model-v1",
        )

        skill = client.post("/api/v1/skills", json={"name": "Governed skill"}).json()
        skill_version = client.post(
            f"/api/v1/skills/{skill['id']}/versions",
            json={"content_ref": "skills/governed/v1", "resource_metadata": {"format": "markdown"}},
        ).json()
        mcp = client.post("/api/v1/mcp-servers", json={"name": "Governed MCP"}).json()
        mcp_version_response = client.post(
            f"/api/v1/mcp-servers/{mcp['id']}/versions",
            json={
                "credential_id": credential["id"],
                "connection_config": {
                    "url": "https://mcp.example.test",
                    "headers": {"X-API-Key": "mcp-secret"},
                    "tools": [{"name": "echo"}],
                },
            },
        )
        self.assertEqual(mcp_version_response.status_code, 201, mcp_version_response.text)
        mcp_version = mcp_version_response.json()
        self.assertEqual(mcp_version["connection_config"]["headers"]["X-API-Key"], "[REDACTED]")
        self.assertNotIn("mcp-secret", mcp_version_response.text)
        policy_set = client.post("/api/v1/policy-sets", json={"name": "Agent policy"}).json()
        policy_version = client.post(
            f"/api/v1/policy-sets/{policy_set['id']}/versions",
            json={"rules": [{"scope": "tool", "decision": "allow", "match": {"name": "echo"}}]},
        ).json()
        agent = client.post("/api/v1/agents", json={"name": "Governed agent"}).json()
        budget = client.post(
            f"/api/v1/agents/{agent['id']}/budgets",
            json={"currency": "USD", "amount_minor_units": 1000, "enforcement_mode": "hard_stop"},
        ).json()
        version_response = client.post(
            f"/api/v1/agents/{agent['id']}/versions",
            json={
                "instructions": "Use only pinned configuration.",
                "model_profile_version_id": profile_v2["id"],
                "default_budget_id": budget["id"],
                "skill_attachments": [{"version_id": skill_version["id"], "config": {"enabled": True}}],
                "mcp_server_attachments": [{"version_id": mcp_version["id"], "config": {"tools": ["echo"]}}],
                "policy_set_version_ids": [policy_version["id"]],
            },
        )
        self.assertEqual(version_response.status_code, 201, version_response.text)
        version = version_response.json()
        self.assertEqual(version["model_profile_id"], profile["id"])
        self.assertEqual(version["model_profile_version_id"], profile_v2["id"])
        self.assertEqual(version["skill_attachments"][0]["version_id"], skill_version["id"])
        self.assertEqual(version["mcp_server_attachments"][0]["version_id"], mcp_version["id"])
        self.assertEqual(version["policy_set_version_ids"], [policy_version["id"]])
        self.assertEqual(client.get(f"/api/v1/agents/{agent['id']}/versions/1").json(), version)

    def test_governed_configuration_rejects_cross_team_and_invalid_references(self) -> None:
        from agentic_os.domain import create_database_engine, session_factory
        from agentic_os.domain.models import Credential, Skill, SkillVersion, Team, User

        engine = create_database_engine(TEST_DATABASE_URL)
        with session_factory(engine)() as session:
            team = Team(name=f"Foreign Team {uuid.uuid4()}")
            user = User(email=f"foreign-{uuid.uuid4()}@example.test", display_name="Foreign owner")
            session.add_all([team, user])
            session.flush()
            credential = Credential(
                team_id=team.id,
                created_by=user.id,
                name="Foreign credential",
                credential_type="api_key",
                encrypted_material="encrypted-foreign-value",
            )
            skill = Skill(team_id=team.id, created_by=user.id, name="Foreign skill")
            session.add_all([credential, skill])
            session.flush()
            skill_version = SkillVersion(skill_id=skill.id, version_number=1, content_ref="skills/foreign/v1")
            session.add(skill_version)
            session.commit()
            credential_id = credential.id
            skill_version_id = skill_version.id
        engine.dispose()

        denied = client.post(
            "/api/v1/model-profiles",
            json={
                "name": "Invalid profile",
                "base_url": "https://example.test/v1",
                "model_identifier": "model",
                "credential_id": str(credential_id),
            },
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertNotIn("encrypted-foreign-value", denied.text)

        agent = client.post("/api/v1/agents", json={"name": "Scope test agent"}).json()
        cross_team = client.post(
            f"/api/v1/agents/{agent['id']}/versions",
            json={"skill_attachments": [{"version_id": str(skill_version_id)}]},
        )
        self.assertEqual(cross_team.status_code, 403, cross_team.text)
        missing = client.post(
            f"/api/v1/agents/{agent['id']}/versions",
            json={"model_profile_version_id": str(uuid.uuid4())},
        )
        self.assertEqual(missing.status_code, 422, missing.text)

    def test_validation_errors_redact_secret_inputs(self) -> None:
        response = client.post(
            "/api/v1/credentials",
            json={"name": "bad", "credential_type": "api_key", "material": {"token": "should-not-echo"}},
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("should-not-echo", response.text)

    def test_state_inspection_reads_persisted_records(self) -> None:
        from agentic_os.domain import create_database_engine, session_factory
        from agentic_os.domain.models import AuditEvent, CostLedgerEntry, Task

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
            session.add(
                CostLedgerEntry(
                    run_id=None,
                    action_type="mcp_tool_call",
                    reserved_amount_minor_units=0,
                    actual_amount_minor_units=0,
                    currency="USD",
                    is_zero_cost=True,
                    status="reconciled",
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

        ledger = client.get("/api/v1/cost-ledger-entries").json()
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["action_type"], "mcp_tool_call")
        self.assertTrue(ledger[0]["is_zero_cost"])

    def _make_goal(self) -> dict:
        project = client.post("/api/v1/projects", json={"name": f"Project {uuid.uuid4()}"}).json()
        return client.post(f"/api/v1/projects/{project['id']}/goals", json={"title": "Ship the feature"}).json()

    def _make_budget(self) -> dict:
        agent = client.post("/api/v1/agents", json={"name": f"Agent {uuid.uuid4()}"}).json()
        return client.post(
            f"/api/v1/agents/{agent['id']}/budgets",
            json={"currency": "USD", "amount_minor_units": 10_000, "enforcement_mode": "warning"},
        ).json()

    def test_create_and_read_task_graph_with_dependencies_and_context(self) -> None:
        goal = self._make_goal()
        budget = self._make_budget()

        response = client.post(
            f"/api/v1/goals/{goal['id']}/task-graph",
            json={
                "tasks": [
                    {
                        "client_id": "research",
                        "title": "Research the approach",
                        "required_capabilities": {"web_search": True},
                        "expected_outputs": [{"name": "research-notes", "kind": "artifact"}],
                        "resource_intent": [{"resource_key": "notes/research.md", "intent": "write"}],
                        "budget_id": budget["id"],
                    },
                    {
                        "client_id": "implement",
                        "title": "Implement the change",
                        "required_capabilities": {"code_edit": True},
                        "resource_intent": [{"resource_key": "src/main.py", "intent": "write"}],
                        "depends_on": ["research"],
                    },
                ]
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(len(body["tasks"]), 2)
        self.assertEqual(len(body["dependencies"]), 1)

        tasks_by_title = {task["title"]: task for task in body["tasks"]}
        research_task = tasks_by_title["Research the approach"]
        implement_task = tasks_by_title["Implement the change"]
        self.assertEqual(research_task["budget_id"], budget["id"])
        self.assertEqual(
            research_task["expected_outputs"],
            [{"name": "research-notes", "kind": "artifact", "description": None}],
        )
        self.assertEqual(
            body["dependencies"],
            [{"task_id": implement_task["id"], "depends_on_task_id": research_task["id"]}],
        )

        read_back = client.get(f"/api/v1/goals/{goal['id']}/task-graph").json()
        self.assertEqual({task["id"] for task in read_back["tasks"]}, {research_task["id"], implement_task["id"]})
        self.assertEqual(read_back["dependencies"], body["dependencies"])

    def test_task_graph_extends_with_stable_identifiers(self) -> None:
        goal = self._make_goal()
        first = client.post(
            f"/api/v1/goals/{goal['id']}/task-graph",
            json={"tasks": [{"client_id": "a", "title": "First task"}]},
        ).json()
        first_task_id = first["tasks"][0]["id"]

        second = client.post(
            f"/api/v1/goals/{goal['id']}/task-graph",
            json={"tasks": [{"client_id": "b", "title": "Second task", "depends_on": [first_task_id]}]},
        )
        self.assertEqual(second.status_code, 201, second.text)
        second_body = second.json()
        second_task_id = next(t["id"] for t in second_body["tasks"] if t["title"] == "Second task")

        graph = client.get(f"/api/v1/goals/{goal['id']}/task-graph").json()
        self.assertEqual(len(graph["tasks"]), 2)
        self.assertIn({"task_id": second_task_id, "depends_on_task_id": first_task_id}, graph["dependencies"])

    def test_task_graph_rejects_cyclic_dependency_graph(self) -> None:
        goal = self._make_goal()
        response = client.post(
            f"/api/v1/goals/{goal['id']}/task-graph",
            json={
                "tasks": [
                    {"client_id": "a", "title": "Task A", "depends_on": ["b"]},
                    {"client_id": "b", "title": "Task B", "depends_on": ["a"]},
                ]
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("cycle", response.json()["detail"])

        graph = client.get(f"/api/v1/goals/{goal['id']}/task-graph").json()
        self.assertEqual(graph["tasks"], [])

    def test_task_graph_diamond_dependencies_do_not_false_positive_on_cycles(self) -> None:
        goal = self._make_goal()
        first = client.post(
            f"/api/v1/goals/{goal['id']}/task-graph",
            json={
                "tasks": [
                    {"client_id": "a", "title": "Task A"},
                    {"client_id": "b", "title": "Task B", "depends_on": ["a"]},
                ]
            },
        ).json()
        task_a_id = next(t["id"] for t in first["tasks"] if t["title"] == "Task A")
        task_b_id = next(t["id"] for t in first["tasks"] if t["title"] == "Task B")

        response = client.post(
            f"/api/v1/goals/{goal['id']}/task-graph",
            json={"tasks": [{"client_id": "d", "title": "Task D", "depends_on": [task_a_id, task_b_id]}]},
        )
        self.assertEqual(response.status_code, 201, response.text)
        graph = client.get(f"/api/v1/goals/{goal['id']}/task-graph").json()
        self.assertEqual(len(graph["tasks"]), 3)
        self.assertEqual(len(graph["dependencies"]), 3)

    def test_task_graph_rejects_malformed_resource_key(self) -> None:
        goal = self._make_goal()
        response = client.post(
            f"/api/v1/goals/{goal['id']}/task-graph",
            json={
                "tasks": [
                    {
                        "client_id": "a",
                        "title": "Bad resource key",
                        "resource_intent": [{"resource_key": "/etc/passwd", "intent": "write"}],
                    }
                ]
            },
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_task_graph_rejects_malformed_required_capabilities(self) -> None:
        goal = self._make_goal()
        response = client.post(
            f"/api/v1/goals/{goal['id']}/task-graph",
            json={"tasks": [{"client_id": "a", "title": "Bad capability", "required_capabilities": {"": True}}]},
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_task_graph_rejects_unknown_budget_reference(self) -> None:
        goal = self._make_goal()
        response = client.post(
            f"/api/v1/goals/{goal['id']}/task-graph",
            json={"tasks": [{"client_id": "a", "title": "Unknown budget", "budget_id": str(uuid.uuid4())}]},
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_task_graph_rejects_unknown_dependency_reference(self) -> None:
        goal = self._make_goal()
        response = client.post(
            f"/api/v1/goals/{goal['id']}/task-graph",
            json={"tasks": [{"client_id": "a", "title": "Dangling dependency", "depends_on": [str(uuid.uuid4())]}]},
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_task_graph_not_found_for_unknown_goal(self) -> None:
        response = client.get(f"/api/v1/goals/{uuid.uuid4()}/task-graph")
        self.assertEqual(response.status_code, 404)

    def test_decompose_goal_creates_inspectable_multi_task_dag(self) -> None:
        goal = self._make_goal()

        response = client.post(f"/api/v1/goals/{goal['id']}/task-graph/decompose", json={})
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(len(body["tasks"]), 3)
        self.assertEqual(len(body["dependencies"]), 2)

        tasks_by_client_title = {task["title"]: task for task in body["tasks"]}
        research_task = tasks_by_client_title[f"Research: {goal['title']}"]
        draft_task = tasks_by_client_title[f"Draft: {goal['title']}"]
        review_task = tasks_by_client_title[f"Review: {goal['title']}"]

        self.assertEqual(research_task["required_capabilities"], {"research": True})
        self.assertIn("research", research_task["capability_rationale"])
        self.assertTrue(research_task["capability_rationale"]["research"]["reason"])
        self.assertTrue(research_task["capability_rationale"]["research"]["evidence"])

        self.assertIn(
            {"task_id": draft_task["id"], "depends_on_task_id": research_task["id"]}, body["dependencies"]
        )
        self.assertIn(
            {"task_id": review_task["id"], "depends_on_task_id": draft_task["id"]}, body["dependencies"]
        )

        read_back = client.get(f"/api/v1/goals/{goal['id']}/task-graph").json()
        self.assertEqual({task["id"] for task in read_back["tasks"]}, {task["id"] for task in body["tasks"]})

        fetched_task = client.get(f"/api/v1/tasks/{research_task['id']}").json()
        self.assertEqual(fetched_task["capability_rationale"], research_task["capability_rationale"])

    def test_decompose_goal_rejects_second_decomposition(self) -> None:
        goal = self._make_goal()
        first = client.post(f"/api/v1/goals/{goal['id']}/task-graph/decompose", json={})
        self.assertEqual(first.status_code, 201, first.text)

        second = client.post(f"/api/v1/goals/{goal['id']}/task-graph/decompose", json={})
        self.assertEqual(second.status_code, 409, second.text)

    def test_decompose_goal_rejects_unsupported_workflow(self) -> None:
        goal = self._make_goal()
        response = client.post(
            f"/api/v1/goals/{goal['id']}/task-graph/decompose", json={"workflow": "not-a-real-workflow"}
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_decompose_goal_not_found_for_unknown_goal(self) -> None:
        response = client.post(f"/api/v1/goals/{uuid.uuid4()}/task-graph/decompose", json={})
        self.assertEqual(response.status_code, 404)

    def test_task_graph_rejects_capability_rationale_for_unrequired_capability(self) -> None:
        goal = self._make_goal()
        response = client.post(
            f"/api/v1/goals/{goal['id']}/task-graph",
            json={
                "tasks": [
                    {
                        "client_id": "a",
                        "title": "Mismatched rationale",
                        "required_capabilities": {"web_search": True},
                        "capability_rationale": {"code_edit": {"reason": "unrelated capability"}},
                    }
                ]
            },
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_agent_version_accepts_known_declared_capabilities(self) -> None:
        agent = client.post("/api/v1/agents", json={"name": f"Agent {uuid.uuid4()}"}).json()
        response = client.post(
            f"/api/v1/agents/{agent['id']}/versions",
            json={"capability_manifest": {"capabilities": ["research", "writing"]}},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["capability_manifest"]["capabilities"], ["research", "writing"])

    def test_agent_version_rejects_unknown_declared_capability(self) -> None:
        agent = client.post("/api/v1/agents", json={"name": f"Agent {uuid.uuid4()}"}).json()
        response = client.post(
            f"/api/v1/agents/{agent['id']}/versions",
            json={"capability_manifest": {"capabilities": ["telekinesis"]}},
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_assignment_api_exposes_selected_version_and_candidate_rationale(self) -> None:
        goal = self._make_goal()
        agent = client.post("/api/v1/agents", json={"name": f"Research Agent {uuid.uuid4()}"}).json()
        version = client.post(
            f"/api/v1/agents/{agent['id']}/versions",
            json={"capability_manifest": {"capabilities": ["research"]}},
        ).json()
        graph = client.post(
            f"/api/v1/goals/{goal['id']}/task-graph",
            json={
                "tasks": [
                    {
                        "client_id": "research",
                        "title": "Research assignment",
                        "required_capabilities": {"research": True},
                    }
                ]
            },
        ).json()
        task_id = graph["tasks"][0]["id"]

        response = client.post(f"/api/v1/tasks/{task_id}/assignment")
        self.assertEqual(response.status_code, 200, response.text)
        assignment = response.json()
        self.assertEqual(assignment["status"], "assigned")
        self.assertIsNotNone(assignment["selected_agent_version_id"])
        self.assertTrue(
            any(candidate["agent_version_id"] == version["id"] for candidate in assignment["candidates"])
        )
        selected = next(
            candidate
            for candidate in assignment["candidates"]
            if candidate["agent_version_id"] == assignment["selected_agent_version_id"]
        )
        self.assertEqual(selected["matched_capabilities"], ["research"])
        self.assertEqual(selected["rejection_reasons"], [])

        inspected = client.get(f"/api/v1/tasks/{task_id}/assignment").json()
        self.assertEqual(inspected, assignment)
        task_state = client.get(f"/api/v1/tasks/{task_id}").json()
        self.assertEqual(task_state["assigned_agent_version_id"], assignment["selected_agent_version_id"])
        self.assertEqual(task_state["assignment_status"], "assigned")


class ArtifactApiTests(unittest.TestCase):
    """Proves project-scoped artifact upload, retrieval, lineage, and access
    checks are backed by persisted PostgreSQL state (issue #17)."""

    def _make_project(self) -> dict:
        return client.post("/api/v1/projects", json={"name": f"Artifact Project {uuid.uuid4()}"}).json()

    def _make_goal(self, project_id: str) -> dict:
        return client.post(f"/api/v1/projects/{project_id}/goals", json={"title": "Ship it"}).json()

    def test_upload_artifact_persists_metadata_and_finalized_version(self) -> None:
        project = self._make_project()
        goal = self._make_goal(project["id"])

        response = client.post(
            f"/api/v1/projects/{project['id']}/artifacts",
            json={
                "name": "notes.md",
                "content": "# Notes\n\nSome durable content.",
                "content_type": "text/markdown",
                "goal_id": goal["id"],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        artifact = response.json()
        self.assertEqual(artifact["project_id"], project["id"])
        self.assertEqual(artifact["goal_id"], goal["id"])
        self.assertEqual(artifact["kind"], "source")
        self.assertEqual(artifact["content_type"], "text/markdown")
        self.assertEqual(artifact["ingestion_status"], "complete")
        self.assertEqual(artifact["ingestion_metadata"]["normalization_version"], "text-v1")
        self.assertIsNone(artifact["ingestion_error"])
        self.assertIsNone(artifact["parent_artifact_id"])
        version = artifact["latest_version"]
        self.assertEqual(version["version_number"], 1)
        self.assertEqual(version["storage_state"], "finalized")
        self.assertTrue(version["content_hash"].startswith("sha256:"))
        self.assertGreater(version["size_bytes"], 0)

        fetched = client.get(f"/api/v1/artifacts/{artifact['id']}").json()
        self.assertEqual(fetched, artifact)

        listed = client.get(f"/api/v1/projects/{project['id']}/artifacts").json()
        self.assertCountEqual([entry["kind"] for entry in listed], ["source", "normalized"])

        normalized = client.get(f"/api/v1/artifacts/{artifact['id']}/normalized")
        self.assertEqual(normalized.status_code, 200, normalized.text)
        normalized_artifact = normalized.json()
        self.assertEqual(normalized_artifact["parent_artifact_id"], artifact["id"])
        self.assertEqual(normalized_artifact["kind"], "normalized")
        self.assertEqual(normalized_artifact["ingestion_metadata"]["headings"][0]["title"], "Notes")
        normalized_content = client.get(f"/api/v1/artifacts/{normalized_artifact['id']}/content")
        self.assertEqual(normalized_content.text, "# Notes\n\nSome durable content.")

    def test_upload_rejects_goal_from_a_different_project(self) -> None:
        project_a = self._make_project()
        project_b = self._make_project()
        goal_b = self._make_goal(project_b["id"])

        response = client.post(
            f"/api/v1/projects/{project_a['id']}/artifacts",
            json={"name": "cross-project.txt", "content": "hello", "goal_id": goal_b["id"]},
        )
        self.assertEqual(response.status_code, 404, response.text)

    def test_get_artifact_not_found_returns_404(self) -> None:
        response = client.get(f"/api/v1/artifacts/{uuid.uuid4()}")
        self.assertEqual(response.status_code, 404)

    def test_artifact_versions_endpoint_lists_immutable_versions(self) -> None:
        project = self._make_project()
        artifact = client.post(
            f"/api/v1/projects/{project['id']}/artifacts",
            json={"name": "log.txt", "content": "line one"},
        ).json()

        versions = client.get(f"/api/v1/artifacts/{artifact['id']}/versions").json()
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["version_number"], 1)
        self.assertEqual(versions[0]["content_hash"], artifact["latest_version"]["content_hash"])

    def test_artifact_content_retrieval_returns_bytes_when_finalized(self) -> None:
        project = self._make_project()
        artifact = client.post(
            f"/api/v1/projects/{project['id']}/artifacts",
            json={"name": "body.txt", "content": "retrievable content", "content_type": "text/plain"},
        ).json()

        response = client.get(f"/api/v1/artifacts/{artifact['id']}/content")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.text, "retrievable content")
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))

    def test_unsupported_upload_preserves_source_and_exposes_status(self) -> None:
        project = self._make_project()
        source = client.post(
            f"/api/v1/projects/{project['id']}/artifacts",
            json={"name": "manual.pdf", "content": "%PDF", "content_type": "application/pdf"},
        ).json()

        self.assertEqual(source["kind"], "source")
        self.assertEqual(source["ingestion_status"], "unsupported")
        self.assertEqual(source["ingestion_metadata"]["reason"], "unsupported content type")
        self.assertEqual(client.get(f"/api/v1/artifacts/{source['id']}/content").text, "%PDF")
        normalized = client.get(f"/api/v1/artifacts/{source['id']}/normalized")
        self.assertEqual(normalized.status_code, 409)
        self.assertIn("unsupported", normalized.json()["detail"])

    def test_artifact_content_retrieval_blocked_when_not_finalized(self) -> None:
        from agentic_os.domain import create_database_engine, session_factory
        from agentic_os.domain.models import ArtifactVersion

        project = self._make_project()
        artifact = client.post(
            f"/api/v1/projects/{project['id']}/artifacts",
            json={"name": "unstable.txt", "content": "will be marked missing"},
        ).json()

        engine = create_database_engine(TEST_DATABASE_URL)
        session_maker = session_factory(engine)
        with session_maker() as session:
            version = session.execute(
                select(ArtifactVersion).where(ArtifactVersion.artifact_id == uuid.UUID(artifact["id"]))
            ).scalar_one()
            version.storage_state = "missing"
            session.commit()
        engine.dispose()

        response = client.get(f"/api/v1/artifacts/{artifact['id']}/content")
        self.assertEqual(response.status_code, 409)

        events = client.get(
            "/api/v1/audit-events", params={"project_id": project["id"]}
        ).json()
        event_types = [event["event_type"] for event in events]
        self.assertIn("artifact.created", event_types)
        self.assertIn("artifact.retrieval_blocked", event_types)

    def test_artifact_lineage_reports_parent_and_children(self) -> None:
        from agentic_os.domain import create_database_engine, session_factory
        from agentic_os.domain.models import Artifact

        project = self._make_project()
        source = client.post(
            f"/api/v1/projects/{project['id']}/artifacts",
            json={"name": "source.md", "content": "# Source"},
        ).json()

        engine = create_database_engine(TEST_DATABASE_URL)
        session_maker = session_factory(engine)
        with session_maker() as session:
            normalized = Artifact(
                project_id=uuid.UUID(project["id"]),
                parent_artifact_id=uuid.UUID(source["id"]),
                name="source.md (normalized)",
                kind="normalized",
                ingestion_status="complete",
            )
            session.add(normalized)
            session.commit()
            normalized_id = normalized.id
        engine.dispose()

        source_lineage = client.get(f"/api/v1/artifacts/{source['id']}/lineage").json()
        self.assertIsNone(source_lineage["parent"])
        self.assertEqual([child["id"] for child in source_lineage["children"]], [str(normalized_id)])

        normalized_lineage = client.get(f"/api/v1/artifacts/{normalized_id}/lineage").json()
        self.assertEqual(normalized_lineage["parent"]["id"], source["id"])
        self.assertEqual(normalized_lineage["children"], [])

    def test_list_artifacts_filters_by_kind(self) -> None:
        from agentic_os.domain import create_database_engine, session_factory
        from agentic_os.domain.models import Artifact

        project = self._make_project()
        client.post(
            f"/api/v1/projects/{project['id']}/artifacts",
            json={"name": "source-only.txt", "content": "hi"},
        )

        engine = create_database_engine(TEST_DATABASE_URL)
        session_maker = session_factory(engine)
        with session_maker() as session:
            session.add(
                Artifact(project_id=uuid.UUID(project["id"]), name="result.json", kind="output")
            )
            session.commit()
        engine.dispose()

        output_only = client.get(
            f"/api/v1/projects/{project['id']}/artifacts", params={"kind": "output"}
        ).json()
        self.assertEqual(len(output_only), 1)
        self.assertEqual(output_only[0]["kind"], "output")

        invalid = client.get(f"/api/v1/projects/{project['id']}/artifacts", params={"kind": "bogus"})
        self.assertEqual(invalid.status_code, 422)


if __name__ == "__main__":
    unittest.main()
