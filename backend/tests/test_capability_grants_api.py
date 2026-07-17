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
from agentic_os.domain.models import (
    AgentVersionMcpServer,
    AgentVersionSkill,
    AuditEvent,
    McpServerInstallation,
    McpServerTool,
    McpServerVersion,
)
from factories import make_team, make_team_membership, make_user

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


def _headers(actor) -> dict[str, str]:
    return {"X-Agentic-User-ID": str(actor.id)}


class CapabilityGrantApiTests(unittest.TestCase):
    def setUp(self) -> None:
        with SessionLocal.begin() as session:
            self.home_team = make_team(session, name=f"Grant Home {uuid.uuid4()}")
            self.owner = make_user(session, display_name="Grant Owner")
            make_team_membership(session, self.home_team, self.owner, role="owner")
            self.other_team = make_team(session, name=f"Grant Other {uuid.uuid4()}")
            self.outsider = make_user(session, display_name="Grant Outsider")
            make_team_membership(session, self.other_team, self.outsider, role="owner")
            for value in (
                self.home_team,
                self.owner,
                self.other_team,
                self.outsider,
            ):
                session.expunge(value)

    def _create_agent(self, actor=None) -> dict:
        response = client.post(
            "/api/v1/agents",
            json={"name": f"Capability Agent {uuid.uuid4()}"},
            headers=_headers(actor or self.owner),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _create_valid_skill_version(self) -> tuple[dict, dict]:
        skill = client.post(
            "/api/v1/skills",
            json={"name": f"Grant Skill {uuid.uuid4()}"},
            headers=_headers(self.owner),
        ).json()
        version = client.post(
            f"/api/v1/skills/{skill['id']}/versions",
            json={
                "manifest": {
                    "name": "grant-skill",
                    "description": "Grant test",
                    "resources": ["references/guide.md"],
                },
                "instructions": "Use the selected guide.",
                "resources": [
                    {
                        "path": "references/guide.md",
                        "content": "Safe guidance",
                    }
                ],
                "declared_capabilities": ["research"],
                "provenance": {
                    "source": "authored",
                    "repository": "https://example.test/skill",
                    "access_token": "must-redact",
                },
            },
            headers=_headers(self.owner),
        )
        self.assertEqual(version.status_code, 201, version.text)
        return skill, version.json()

    def _create_mcp_version(
        self,
        *,
        actor=None,
        visibility: str = "private",
        credential: bool = True,
        credential_required: bool = True,
        enabled: bool = True,
    ) -> tuple[dict, dict]:
        actor = actor or self.owner
        server = client.post(
            "/api/v1/mcp-servers",
            json={"name": f"Grant MCP {uuid.uuid4()}", "visibility": visibility},
            headers=_headers(actor),
        ).json()
        payload = {
            "connection_config": {
                "url": "https://mcp.example.test",
                "credential_required": credential_required,
            }
        }
        if credential:
            payload["credential"] = "credential-must-not-leak"
        version_response = client.post(
            f"/api/v1/mcp-servers/{server['id']}/versions",
            json=payload,
            headers=_headers(actor),
        )
        self.assertEqual(version_response.status_code, 201, version_response.text)
        version = version_response.json()
        with SessionLocal.begin() as session:
            session.add(
                McpServerTool(
                    mcp_server_version_id=uuid.UUID(version["id"]),
                    tool_name="echo",
                    description="Untrusted remote description",
                    input_schema={"type": "object"},
                    schema_valid=True,
                    schema_validation_errors=[],
                    descriptor_hash="d" * 64,
                    credential_scope_required=credential_required,
                    enabled=enabled,
                    timeout_ms=2500,
                    output_limit_bytes=4096,
                )
            )
        return server, version

    def test_skill_resource_grant_is_validated_redacted_and_attributed(self) -> None:
        _, skill_version = self._create_valid_skill_version()
        agent = self._create_agent()
        response = client.post(
            f"/api/v1/agents/{agent['id']}/versions",
            json={
                "skill_grants": [
                    {
                        "version_id": skill_version["id"],
                        "resource_paths": ["references/guide.md"],
                        "policy_metadata": {
                            "decision": "allow",
                            "api_key": "policy-secret",
                        },
                    }
                ]
            },
            headers=_headers(self.owner),
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn("policy-secret", response.text)
        self.assertNotIn("must-redact", response.text)
        grant = response.json()["skill_grants"][0]
        self.assertEqual(grant["resource_paths"], ["references/guide.md"])
        self.assertEqual(grant["declared_capabilities"], ["research"])
        self.assertEqual(grant["granted_by"], str(self.owner.id))
        self.assertNotIn("access_token", grant["provenance"])
        self.assertEqual(grant["policy_metadata"]["api_key"], "[REDACTED]")

        with SessionLocal() as session:
            row = session.execute(
                select(AgentVersionSkill).where(
                    AgentVersionSkill.skill_version_id
                    == uuid.UUID(skill_version["id"])
                )
            ).scalar_one()
            self.assertEqual(row.granted_by, self.owner.id)
            event = session.execute(
                select(AuditEvent)
                .where(AuditEvent.event_type == "agent.capability_grants.created")
                .order_by(AuditEvent.sequence_number.desc())
            ).scalars().first()
            self.assertEqual(event.payload["actor_id"], str(self.owner.id))
            self.assertNotIn("policy-secret", str(event.payload))

    def test_skill_grant_rejects_legacy_and_unknown_resources(self) -> None:
        skill = client.post(
            "/api/v1/skills",
            json={"name": f"Legacy {uuid.uuid4()}"},
            headers=_headers(self.owner),
        ).json()
        legacy = client.post(
            f"/api/v1/skills/{skill['id']}/versions",
            json={"content_ref": "skills/legacy/v1"},
            headers=_headers(self.owner),
        ).json()
        agent = self._create_agent()
        invalid = client.post(
            f"/api/v1/agents/{agent['id']}/versions",
            json={"skill_grants": [{"version_id": legacy["id"]}]},
            headers=_headers(self.owner),
        )
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(invalid.json()["detail"]["code"], "skill_version_not_valid")

        _, package = self._create_valid_skill_version()
        missing = client.post(
            f"/api/v1/agents/{agent['id']}/versions",
            json={
                "skill_grants": [
                    {
                        "version_id": package["id"],
                        "resource_paths": ["references/missing.md"],
                    }
                ]
            },
            headers=_headers(self.owner),
        )
        self.assertEqual(missing.status_code, 422, missing.text)
        self.assertEqual(missing.json()["detail"]["code"], "skill_resource_not_found")

    def test_mcp_tool_grant_pins_descriptor_limits_and_requires_credentials(self) -> None:
        _, version = self._create_mcp_version()
        agent = self._create_agent()
        response = client.post(
            f"/api/v1/agents/{agent['id']}/versions",
            json={
                "mcp_tool_grants": [
                    {
                        "version_id": version["id"],
                        "tool_names": ["echo"],
                        "policy_metadata": {
                            "approval": "required",
                            "token": "policy-secret",
                        },
                    }
                ]
            },
            headers=_headers(self.owner),
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn("credential-must-not-leak", response.text)
        self.assertNotIn("policy-secret", response.text)
        grant = response.json()["mcp_tool_grants"][0]
        self.assertTrue(grant["credential_configured"])
        self.assertEqual(grant["granted_by"], str(self.owner.id))
        self.assertEqual(
            grant["tools"],
            [
                {
                    "name": "echo",
                    "descriptor_hash": "d" * 64,
                    "timeout_ms": 2500,
                    "output_limit_bytes": 4096,
                }
            ],
        )
        with SessionLocal() as session:
            row = session.execute(
                select(AgentVersionMcpServer).where(
                    AgentVersionMcpServer.mcp_server_version_id
                    == uuid.UUID(version["id"])
                )
            ).scalar_one()
            self.assertEqual(row.granted_by, self.owner.id)

    def test_mcp_grant_rejects_disabled_missing_credential_and_revoked_access(self) -> None:
        _, disabled_version = self._create_mcp_version(enabled=False)
        agent = self._create_agent()
        disabled = client.post(
            f"/api/v1/agents/{agent['id']}/versions",
            json={
                "mcp_tool_grants": [
                    {
                        "version_id": disabled_version["id"],
                        "tool_names": ["echo"],
                    }
                ]
            },
            headers=_headers(self.owner),
        )
        self.assertEqual(disabled.status_code, 422, disabled.text)
        self.assertEqual(disabled.json()["detail"]["code"], "mcp_tool_unavailable")

        _, missing_credential_version = self._create_mcp_version(credential=False)
        missing = client.post(
            f"/api/v1/agents/{agent['id']}/versions",
            json={
                "mcp_tool_grants": [
                    {
                        "version_id": missing_credential_version["id"],
                        "tool_names": ["echo"],
                    }
                ]
            },
            headers=_headers(self.owner),
        )
        self.assertEqual(missing.status_code, 422, missing.text)
        self.assertEqual(missing.json()["detail"]["code"], "mcp_credential_missing")

        server, public_version = self._create_mcp_version(
            actor=self.owner,
            visibility="public",
            credential=False,
            credential_required=False,
        )
        outsider_agent = self._create_agent(self.outsider)
        private = client.patch(
            f"/api/v1/mcp-servers/{server['id']}",
            json={"visibility": "private"},
            headers=_headers(self.owner),
        )
        self.assertEqual(private.status_code, 200, private.text)
        revoked = client.post(
            f"/api/v1/agents/{outsider_agent['id']}/versions",
            json={
                "mcp_tool_grants": [
                    {
                        "version_id": public_version["id"],
                        "tool_names": ["echo"],
                    }
                ]
            },
            headers=_headers(self.outsider),
        )
        self.assertEqual(revoked.status_code, 403, revoked.text)
        self.assertEqual(
            revoked.json()["detail"]["code"], "mcp_definition_access_revoked"
        )

    def test_public_mcp_install_copies_definition_without_credentials_or_authority(self) -> None:
        server, version = self._create_mcp_version(visibility="public")
        installed = client.post(
            f"/api/v1/mcp-servers/{server['id']}/versions/1/install",
            json={"name": "Installed MCP"},
            headers=_headers(self.outsider),
        )
        self.assertEqual(installed.status_code, 201, installed.text)
        self.assertNotIn("credential-must-not-leak", installed.text)
        installed_body = installed.json()
        self.assertEqual(installed_body["team_id"], str(self.other_team.id))
        self.assertEqual(installed_body["visibility"], "private")

        installed_version = client.get(
            f"/api/v1/mcp-servers/{installed_body['id']}/versions/1",
            headers=_headers(self.outsider),
        )
        self.assertEqual(installed_version.status_code, 200, installed_version.text)
        self.assertFalse(installed_version.json()["credential_configured"])
        tools = client.get(
            f"/api/v1/mcp-servers/{installed_body['id']}/versions/1/discovered-tools",
            headers=_headers(self.outsider),
        )
        self.assertEqual(tools.status_code, 200, tools.text)
        self.assertEqual(tools.json()[0]["descriptor_hash"], "d" * 64)

        lineage = client.get(
            f"/api/v1/mcp-servers/{installed_body['id']}/installation",
            headers=_headers(self.outsider),
        )
        self.assertEqual(lineage.status_code, 200, lineage.text)
        self.assertEqual(
            lineage.json()["source_mcp_server_version_id"], version["id"]
        )
        with SessionLocal() as session:
            record = session.execute(
                select(McpServerInstallation).where(
                    McpServerInstallation.installed_mcp_server_id
                    == uuid.UUID(installed_body["id"])
                )
            ).scalar_one()
            copied_version = session.execute(
                select(McpServerVersion).where(
                    McpServerVersion.mcp_server_id
                    == uuid.UUID(installed_body["id"])
                )
            ).scalar_one()
            self.assertEqual(record.installed_by, self.outsider.id)
            self.assertIsNone(copied_version.credential_id)
            self.assertIsNone(copied_version.credential_ciphertext)

        source_owner_denied = client.patch(
            f"/api/v1/mcp-servers/{installed_body['id']}",
            json={"name": "Reclaimed"},
            headers=_headers(self.owner),
        )
        self.assertEqual(source_owner_denied.status_code, 404)
        installer_denied = client.patch(
            f"/api/v1/mcp-servers/{server['id']}",
            json={"name": "Hijacked"},
            headers=_headers(self.outsider),
        )
        self.assertEqual(installer_denied.status_code, 404)


if __name__ == "__main__":
    unittest.main()
