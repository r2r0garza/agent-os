"""Sprint 8 exit criterion 6: restart durability for access/sharing evidence.

These tests seed real team membership, project grant, installed-definition
lineage, and MCP credential attachment/revocation through the API, then
simulate a process restart by disposing the cached engine and rebuilding the
FastAPI app against the same durable PostgreSQL database. A fresh client must
still observe the same grants, redacted credential state, and audit trail.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from sqlalchemy import create_engine

from agentic_os.domain import create_database_engine, database_url, session_factory
from factories import make_project, make_team, make_team_membership, make_user

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

BACKEND_ROOT = Path(__file__).parents[1]


def setUpModule() -> None:
    global client, SessionLocal, DATABASE_URL
    DATABASE_URL = os.environ.get("AGENTIC_OS_DATABASE_URL", database_url())
    probe = create_database_engine(DATABASE_URL)
    try:
        with probe.connect():
            pass
    except Exception as error:  # pragma: no cover - environment guard
        raise unittest.SkipTest(f"PostgreSQL is not reachable: {error}")
    finally:
        probe.dispose()
    engine = create_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    engine.dispose()
    subprocess.run(
        [str(BACKEND_ROOT / ".venv" / "bin" / "alembic"), "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=dict(os.environ, AGENTIC_OS_DATABASE_URL=DATABASE_URL),
        check=True,
        capture_output=True,
        text=True,
    )
    os.environ["AGENTIC_OS_DATABASE_URL"] = DATABASE_URL
    client = _fresh_client()
    SessionLocal = session_factory(create_database_engine(DATABASE_URL))


def _fresh_client():
    """Rebuild the FastAPI app against a fresh, uncached engine.

    Mirrors the restart boundary in test_restart_recovery.py: the durable
    record lives in PostgreSQL, so a new app/engine standing in for a
    restarted process must observe exactly what a prior process committed.
    """
    from agentic_os.api.deps import _engine

    if _engine.cache_info().currsize:
        _engine().dispose()
        _engine.cache_clear()
    from fastapi.testclient import TestClient

    from agentic_os.api.app import create_app

    return TestClient(create_app())


def _headers(actor) -> dict[str, str]:
    return {"X-Agentic-User-ID": str(actor.id)}


class AccessSharingRestartDurabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        with SessionLocal.begin() as session:
            self.source_team = make_team(session, name=f"Source {uuid.uuid4()}")
            self.owner = make_user(session, display_name="Owner")
            make_team_membership(session, self.source_team, self.owner, role="owner")
            self.teammate = make_user(session, display_name="Teammate")
            make_team_membership(session, self.source_team, self.teammate)

            self.installing_team = make_team(session, name=f"Installer {uuid.uuid4()}")
            self.installer = make_user(session, display_name="Installer")
            make_team_membership(session, self.installing_team, self.installer, role="owner")

            self.outsider_team = make_team(session, name=f"Outsider {uuid.uuid4()}")
            self.outsider = make_user(session, display_name="Outsider")
            make_team_membership(session, self.outsider_team, self.outsider)

            self.project = make_project(session, self.source_team, self.owner, name="Durable project")

            for value in (
                self.source_team,
                self.owner,
                self.teammate,
                self.installing_team,
                self.installer,
                self.outsider_team,
                self.outsider,
                self.project,
            ):
                session.expunge(value)

        self.secret = f"durable-secret-{uuid.uuid4()}"

        # Project grant attribution.
        granted = client.post(
            f"/api/v1/projects/{self.project.id}/members",
            json={"user_id": str(self.teammate.id)},
            headers=_headers(self.owner),
        )
        self.assertEqual(granted.status_code, 201, granted.text)

        # Public agent + installed-definition lineage.
        source_agent = client.post(
            "/api/v1/agents",
            json={"name": f"Shared Agent {uuid.uuid4()}", "visibility": "public"},
            headers=_headers(self.owner),
        )
        self.assertEqual(source_agent.status_code, 201, source_agent.text)
        self.source_agent_id = source_agent.json()["id"]
        version = client.post(
            f"/api/v1/agents/{self.source_agent_id}/versions",
            json={"instructions": "Do the durable thing", "capability_manifest": {}},
            headers=_headers(self.owner),
        )
        self.assertEqual(version.status_code, 201, version.text)
        installed = client.post(
            f"/api/v1/agents/{self.source_agent_id}/versions/1/install",
            json={"name": "Installed copy"},
            headers=_headers(self.installer),
        )
        self.assertEqual(installed.status_code, 201, installed.text)
        self.installed_agent_id = installed.json()["id"]

        # MCP server, scoped credential attachment, and revocation.
        server = client.post(
            "/api/v1/mcp-servers",
            json={"name": f"Durable MCP {uuid.uuid4()}", "visibility": "public"},
            headers=_headers(self.owner),
        )
        self.assertEqual(server.status_code, 201, server.text)
        self.server_id = server.json()["id"]
        server_version = client.post(
            f"/api/v1/mcp-servers/{self.server_id}/versions",
            json={
                "credential": self.secret,
                "connection_config": {
                    "url": "https://mcp.example.test/durable",
                    "headers": {"Authorization": self.secret},
                    "tools": [{"name": "echo"}],
                },
            },
            headers=_headers(self.owner),
        )
        self.assertEqual(server_version.status_code, 201, server_version.text)
        credential = client.post(
            "/api/v1/credentials",
            json={
                "name": "Durable project credential",
                "credential_type": "api_key",
                "material": self.secret,
                "project_id": str(self.project.id),
            },
            headers=_headers(self.owner),
        ).json()
        attachment = client.post(
            f"/api/v1/mcp-servers/{self.server_id}/versions/1/attachments",
            json={"project_id": str(self.project.id), "credential_id": credential["id"]},
            headers=_headers(self.owner),
        )
        self.assertEqual(attachment.status_code, 201, attachment.text)
        self.attachment_id = attachment.json()["id"]

        second_attachment = client.post(
            f"/api/v1/mcp-servers/{self.server_id}/versions/1/attachments",
            json={"project_id": str(self.project.id), "credential_id": credential["id"]},
            headers=_headers(self.owner),
        )
        self.assertEqual(second_attachment.status_code, 201, second_attachment.text)
        self.revoked_attachment_id = second_attachment.json()["id"]
        revoke = client.delete(
            f"/api/v1/mcp-servers/{self.server_id}/versions/1/attachments/{self.revoked_attachment_id}",
            headers=_headers(self.owner),
        )
        self.assertEqual(revoke.status_code, 200, revoke.text)

        # A denied read produces a redacted authorization-decision audit event.
        denied = client.get(
            f"/api/v1/projects/{self.project.id}", headers=_headers(self.outsider)
        )
        self.assertEqual(denied.status_code, 404)

    def test_grants_lineage_and_audit_evidence_survive_simulated_restart(self) -> None:
        global client
        client = _fresh_client()

        members = client.get(
            f"/api/v1/projects/{self.project.id}/members", headers=_headers(self.owner)
        )
        self.assertEqual(members.status_code, 200, members.text)
        member_ids = {item["user_id"] for item in members.json()}
        self.assertIn(str(self.teammate.id), member_ids)

        installed_detail = client.get(
            f"/api/v1/agents/{self.installed_agent_id}", headers=_headers(self.installer)
        )
        self.assertEqual(installed_detail.status_code, 200, installed_detail.text)
        self.assertNotEqual(installed_detail.json()["id"], self.source_agent_id)

        attachments = client.get(
            f"/api/v1/mcp-servers/{self.server_id}/versions/1/attachments",
            headers=_headers(self.owner),
        )
        self.assertEqual(attachments.status_code, 200, attachments.text)
        by_id = {item["id"]: item for item in attachments.json()}
        self.assertTrue(by_id[self.attachment_id]["credential_configured"])
        self.assertFalse(by_id[self.attachment_id]["revoked"])
        self.assertTrue(by_id[self.revoked_attachment_id]["revoked"])
        self.assertNotIn(self.secret, attachments.text)

        audit = client.get(
            "/api/v1/audit-events",
            params={"project_id": str(self.project.id)},
            headers=_headers(self.owner),
        )
        self.assertEqual(audit.status_code, 200, audit.text)
        self.assertNotIn(self.secret, audit.text)
        event_types = {item["event_type"] for item in audit.json()}
        self.assertIn("project.member.granted", event_types)
        self.assertIn("mcp.attachment.created", event_types)
        self.assertIn("mcp.attachment.revoked", event_types)

        with SessionLocal() as session:
            from sqlalchemy import select

            from agentic_os.domain.models import AuditEvent

            deny_event = session.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.event_type == "authorization.decision",
                    AuditEvent.payload["actor_id"].as_string() == str(self.outsider.id),
                    AuditEvent.payload["decision"].as_string() == "deny",
                )
                .order_by(AuditEvent.sequence_number.desc())
            ).scalars().first()
            self.assertIsNotNone(deny_event)
            self.assertNotIn(str(self.project.id), str(deny_event.payload))


if __name__ == "__main__":
    unittest.main()
