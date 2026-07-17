from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from sqlalchemy import create_engine, select

from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import Agent, Artifact, AuditEvent, Goal, Run, Skill, Task
from factories import (
    make_project,
    make_project_member,
    make_team,
    make_team_membership,
    make_user,
)

BACKEND_ROOT = Path(__file__).parents[1]


def setUpModule() -> None:
    global client, SessionLocal
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
        [str(BACKEND_ROOT / ".venv" / "bin" / "alembic"), "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=dict(os.environ, AGENTIC_OS_DATABASE_URL=db_url),
        check=True,
        capture_output=True,
        text=True,
    )
    os.environ["AGENTIC_OS_DATABASE_URL"] = db_url

    from agentic_os.api.deps import _engine

    if _engine.cache_info().currsize:
        _engine().dispose()
        _engine.cache_clear()
    from fastapi.testclient import TestClient

    from agentic_os.api.app import create_app

    client = TestClient(create_app())
    SessionLocal = session_factory(create_database_engine(db_url))


class AuthorizationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        with SessionLocal.begin() as session:
            self.team = make_team(session, name=f"Owner team {uuid.uuid4()}")
            self.owner = make_user(session, display_name="Owner")
            make_team_membership(session, self.team, self.owner, role="owner")
            self.grantee = make_user(session, display_name="Grantee")
            make_team_membership(session, self.team, self.grantee)
            self.ungranted = make_user(session, display_name="Ungranted")
            make_team_membership(session, self.team, self.ungranted)

            other_team = make_team(session, name=f"Other team {uuid.uuid4()}")
            self.outsider = make_user(session, display_name="Outsider")
            make_team_membership(session, other_team, self.outsider)
            self.admin = make_user(session, display_name="Admin", role="admin")

            self.project = make_project(session, self.team, self.owner, name="Protected")
            make_project_member(session, self.project, self.grantee, granted_by=self.owner)
            self.goal = Goal(project_id=self.project.id, created_by=self.owner.id, title="Goal")
            session.add(self.goal)
            session.flush()
            self.task = Task(goal_id=self.goal.id, created_by=self.owner.id, title="Task")
            session.add(self.task)
            session.flush()
            self.agent = Agent(team_id=self.team.id, created_by=self.owner.id, name="Agent")
            self.skill = Skill(team_id=self.team.id, created_by=self.owner.id, name="Skill")
            session.add_all([self.agent, self.skill])
            session.flush()
            from agentic_os.domain.models import AgentVersion

            agent_version = AgentVersion(agent_id=self.agent.id, version_number=1)
            session.add(agent_version)
            session.flush()
            self.run = Run(
                task_id=self.task.id,
                attempt_number=1,
                idempotency_key=f"run-{uuid.uuid4()}",
                lease_token=1,
                agent_version_id=agent_version.id,
                status="queued",
            )
            session.add(self.run)
            self.artifact = Artifact(
                project_id=self.project.id,
                goal_id=self.goal.id,
                task_id=self.task.id,
                created_by=self.owner.id,
                name="Evidence",
            )
            session.add(self.artifact)
            session.flush()

            for value in (
                self.team,
                self.owner,
                self.grantee,
                self.ungranted,
                self.outsider,
                self.admin,
                self.project,
                self.goal,
                self.task,
                self.run,
                self.artifact,
                self.agent,
                self.skill,
            ):
                session.expunge(value)

    @staticmethod
    def _headers(actor) -> dict[str, str]:
        return {"X-Agentic-User-ID": str(actor.id)}

    def test_inherited_project_access_covers_goal_task_run_and_artifact(self) -> None:
        paths = (
            f"/api/v1/projects/{self.project.id}",
            f"/api/v1/goals/{self.goal.id}",
            f"/api/v1/tasks/{self.task.id}",
            f"/api/v1/runs/{self.run.id}",
            f"/api/v1/artifacts/{self.artifact.id}",
        )
        for path in paths:
            self.assertEqual(client.get(path, headers=self._headers(self.grantee)).status_code, 200)
            self.assertEqual(client.get(path, headers=self._headers(self.ungranted)).status_code, 404)
            self.assertEqual(client.get(path, headers=self._headers(self.outsider)).status_code, 404)
            self.assertEqual(client.get(path, headers=self._headers(self.admin)).status_code, 200)

    def test_lists_are_scoped_and_supplied_unknown_identity_fails_closed(self) -> None:
        listed = client.get("/api/v1/projects", headers=self._headers(self.grantee))
        self.assertEqual([item["id"] for item in listed.json()], [str(self.project.id)])
        self.assertEqual(
            client.get("/api/v1/projects", headers=self._headers(self.ungranted)).json(), []
        )
        self.assertEqual(
            client.get("/api/v1/agents", headers=self._headers(self.outsider)).json(), []
        )
        self.assertEqual(
            client.get("/api/v1/projects", headers={"X-Agentic-User-ID": "not-a-uuid"}).status_code,
            401,
        )
        self.assertEqual(
            client.get(
                "/api/v1/projects", headers={"X-Agentic-User-ID": str(uuid.uuid4())}
            ).status_code,
            401,
        )

    def test_public_mcp_definition_does_not_share_scoped_credentials(self) -> None:
        secret = "mcp-secret-that-must-not-leak"
        server_response = client.post(
            "/api/v1/mcp-servers",
            json={"name": "Public MCP", "visibility": "public"},
            headers=self._headers(self.owner),
        )
        self.assertEqual(server_response.status_code, 201, server_response.text)
        server = server_response.json()
        version_response = client.post(
            f"/api/v1/mcp-servers/{server['id']}/versions",
            json={
                "credential": secret,
                "connection_config": {
                    "url": "https://mcp.example.test",
                    "headers": {"Authorization": secret},
                    "tools": [{"name": "echo"}],
                },
            },
            headers=self._headers(self.owner),
        )
        self.assertEqual(version_response.status_code, 201, version_response.text)
        owner_version = version_response.json()
        self.assertTrue(owner_version["credential_configured"])
        self.assertNotIn(secret, version_response.text)

        outsider_version = client.get(
            f"/api/v1/mcp-servers/{server['id']}/versions/1",
            headers=self._headers(self.outsider),
        )
        self.assertEqual(outsider_version.status_code, 200, outsider_version.text)
        self.assertFalse(outsider_version.json()["credential_configured"])
        self.assertIsNone(outsider_version.json()["credential_id"])
        self.assertNotIn(secret, outsider_version.text)

        outsider_credential = client.post(
            "/api/v1/credentials",
            json={
                "name": "Outsider MCP credential",
                "credential_type": "api_key",
                "material": "outsider-secret",
            },
            headers=self._headers(self.outsider),
        ).json()
        attachment_response = client.post(
            f"/api/v1/mcp-servers/{server['id']}/versions/1/attachments",
            json={
                "team_id": outsider_credential["team_id"],
                "credential_id": outsider_credential["id"],
            },
            headers=self._headers(self.outsider),
        )
        self.assertEqual(attachment_response.status_code, 201, attachment_response.text)
        self.assertTrue(attachment_response.json()["credential_configured"])
        self.assertNotIn("outsider-secret", attachment_response.text)

        owner_credential = client.post(
            "/api/v1/credentials",
            json={
                "name": "Owner-only MCP credential",
                "credential_type": "api_key",
                "material": "owner-secret",
            },
            headers=self._headers(self.owner),
        ).json()
        denied = client.post(
            f"/api/v1/mcp-servers/{server['id']}/versions/1/attachments",
            json={
                "team_id": outsider_credential["team_id"],
                "credential_id": owner_credential["id"],
            },
            headers=self._headers(self.outsider),
        )
        # Definition access is public, but another scope's credential remains inaccessible.
        self.assertIn(denied.status_code, (404, 422))

        project_credential = client.post(
            "/api/v1/credentials",
            json={
                "name": "Project MCP credential",
                "credential_type": "api_key",
                "material": "project-secret",
                "project_id": str(self.project.id),
            },
            headers=self._headers(self.owner),
        ).json()
        project_attachment = client.post(
            f"/api/v1/mcp-servers/{server['id']}/versions/1/attachments",
            json={
                "project_id": str(self.project.id),
                "credential_id": project_credential["id"],
            },
            headers=self._headers(self.owner),
        )
        self.assertEqual(project_attachment.status_code, 201, project_attachment.text)
        audit = client.get(
            "/api/v1/audit-events",
            params={"project_id": str(self.project.id)},
            headers=self._headers(self.owner),
        )
        self.assertEqual(audit.status_code, 200, audit.text)
        self.assertNotIn("project-secret", audit.text)
        self.assertTrue(
            any(item["event_type"] == "mcp.attachment.created" for item in audit.json())
        )

    def test_actor_attribution_and_default_operator_compatibility(self) -> None:
        created = client.post(
            "/api/v1/projects",
            json={"name": "Attributed"},
            headers=self._headers(self.owner),
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["created_by"], str(self.owner.id))
        goal = client.post(
            f"/api/v1/projects/{created.json()['id']}/goals",
            json={"title": "Attributed goal"},
            headers=self._headers(self.owner),
        )
        self.assertEqual(goal.status_code, 201, goal.text)
        self.assertEqual(goal.json()["created_by"], str(self.owner.id))

        local = client.post("/api/v1/projects", json={"name": "Local operator"})
        self.assertEqual(local.status_code, 201, local.text)

    def test_denial_audit_is_redacted_and_admin_health_is_installation_wide(self) -> None:
        denied = client.get(
            f"/api/v1/projects/{self.project.id}", headers=self._headers(self.outsider)
        )
        self.assertEqual(denied.status_code, 404)
        self.assertNotIn(str(self.project.id), denied.text)
        admin_health = client.get(
            "/api/v1/admin/observability/health", headers=self._headers(self.admin)
        )
        self.assertEqual(admin_health.status_code, 200, admin_health.text)
        inherited_audit = client.get(
            "/api/v1/audit-events",
            params={"goal_id": str(self.goal.id)},
            headers=self._headers(self.grantee),
        )
        self.assertEqual(inherited_audit.status_code, 200, inherited_audit.text)
        with SessionLocal() as session:
            decision = session.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.event_type == "authorization.decision",
                    AuditEvent.payload["actor_id"].as_string() == str(self.outsider.id),
                    AuditEvent.payload["decision"].as_string() == "deny",
                )
                .order_by(AuditEvent.sequence_number.desc())
            ).scalars().first()
            self.assertIsNotNone(decision)
            self.assertEqual(decision.payload["decision"], "deny")
            self.assertEqual(decision.payload["actor_id"], str(self.outsider.id))
            self.assertTrue(decision.payload["redaction_evidence"]["resource_identifier_redacted"])
            self.assertNotIn(str(self.project.id), str(decision.payload))
            admin_allow = session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "authorization.decision",
                    AuditEvent.payload["actor_id"].as_string() == str(self.admin.id),
                    AuditEvent.payload["decision"].as_string() == "allow",
                )
            ).scalars().first()
            self.assertIsNotNone(admin_allow)


if __name__ == "__main__":
    unittest.main()
