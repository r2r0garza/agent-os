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
    Agent,
    AgentInstallation,
    AgentVersion,
    Skill,
    SkillInstallation,
    SkillVersion,
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


class DefinitionVisibilityTests(unittest.TestCase):
    """Sprint 8 exit criterion 3: visibility rules gate list/detail/update/delete."""

    def setUp(self) -> None:
        with SessionLocal.begin() as session:
            self.home_team = make_team(session, name=f"Home {uuid.uuid4()}")
            self.creator = make_user(session, display_name="Creator")
            make_team_membership(session, self.home_team, self.creator, role="owner")
            self.teammate = make_user(session, display_name="Teammate")
            make_team_membership(session, self.home_team, self.teammate)

            self.other_team = make_team(session, name=f"Other {uuid.uuid4()}")
            self.outsider = make_user(session, display_name="Outsider")
            make_team_membership(session, self.other_team, self.outsider)
            self.admin = make_user(session, display_name="Admin", role="admin")

            for value in (
                self.home_team,
                self.creator,
                self.teammate,
                self.other_team,
                self.outsider,
                self.admin,
            ):
                session.expunge(value)

    def _create_agent(self, actor, *, visibility: str = "private", name: str | None = None) -> dict:
        created = client.post(
            "/api/v1/agents",
            json={"name": name or f"Agent {uuid.uuid4()}", "visibility": visibility},
            headers=_headers(actor),
        )
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    def _create_skill(self, actor, *, visibility: str = "private", name: str | None = None) -> dict:
        created = client.post(
            "/api/v1/skills",
            json={"name": name or f"Skill {uuid.uuid4()}", "visibility": visibility},
            headers=_headers(actor),
        )
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    def test_private_definition_is_invisible_outside_home_team(self) -> None:
        agent = self._create_agent(self.creator, visibility="private")
        skill = self._create_skill(self.creator, visibility="private")

        self.assertEqual(
            client.get(f"/api/v1/agents/{agent['id']}", headers=_headers(self.outsider)).status_code, 404
        )
        self.assertEqual(
            client.get(f"/api/v1/skills/{skill['id']}", headers=_headers(self.outsider)).status_code, 404
        )
        self.assertNotIn(agent["id"], [item["id"] for item in client.get("/api/v1/agents", headers=_headers(self.outsider)).json()])
        self.assertNotIn(skill["id"], [item["id"] for item in client.get("/api/v1/skills", headers=_headers(self.outsider)).json()])

        # Home team members always retain access regardless of visibility.
        self.assertEqual(
            client.get(f"/api/v1/agents/{agent['id']}", headers=_headers(self.teammate)).status_code, 200
        )
        # Admin always retains access.
        self.assertEqual(
            client.get(f"/api/v1/agents/{agent['id']}", headers=_headers(self.admin)).status_code, 200
        )

    def test_team_visible_definition_is_readable_but_unlisted_cross_team(self) -> None:
        agent = self._create_agent(self.creator, visibility="team")

        detail = client.get(f"/api/v1/agents/{agent['id']}", headers=_headers(self.outsider))
        self.assertEqual(detail.status_code, 200, detail.text)

        listed = client.get("/api/v1/agents", headers=_headers(self.outsider)).json()
        self.assertNotIn(agent["id"], [item["id"] for item in listed])

    def test_public_definition_is_readable_and_listed_cross_team(self) -> None:
        agent = self._create_agent(self.creator, visibility="public")
        skill = self._create_skill(self.creator, visibility="public")

        self.assertEqual(
            client.get(f"/api/v1/agents/{agent['id']}", headers=_headers(self.outsider)).status_code, 200
        )
        self.assertEqual(
            client.get(f"/api/v1/skills/{skill['id']}", headers=_headers(self.outsider)).status_code, 200
        )
        agent_listed = client.get("/api/v1/agents", headers=_headers(self.outsider)).json()
        self.assertIn(agent["id"], [item["id"] for item in agent_listed])
        skill_listed = client.get("/api/v1/skills", headers=_headers(self.outsider)).json()
        self.assertIn(skill["id"], [item["id"] for item in skill_listed])

    def test_cross_team_read_access_never_grants_edit_rights(self) -> None:
        agent = self._create_agent(self.creator, visibility="public")
        skill = self._create_skill(self.creator, visibility="public")

        denied_patch = client.patch(
            f"/api/v1/agents/{agent['id']}", json={"name": "Hijacked"}, headers=_headers(self.outsider)
        )
        self.assertEqual(denied_patch.status_code, 404, denied_patch.text)
        denied_skill_patch = client.patch(
            f"/api/v1/skills/{skill['id']}", json={"name": "Hijacked"}, headers=_headers(self.outsider)
        )
        self.assertEqual(denied_skill_patch.status_code, 404, denied_skill_patch.text)
        denied_version = client.post(
            f"/api/v1/agents/{agent['id']}/versions", json={}, headers=_headers(self.outsider)
        )
        self.assertEqual(denied_version.status_code, 404, denied_version.text)
        denied_delete = client.delete(f"/api/v1/agents/{agent['id']}", headers=_headers(self.outsider))
        self.assertEqual(denied_delete.status_code, 404, denied_delete.text)

    def test_only_owner_or_admin_can_change_visibility(self) -> None:
        agent = self._create_agent(self.creator, visibility="private")

        denied = client.patch(
            f"/api/v1/agents/{agent['id']}", json={"visibility": "public"}, headers=_headers(self.teammate)
        )
        self.assertEqual(denied.status_code, 403, denied.text)

        allowed_by_owner = client.patch(
            f"/api/v1/agents/{agent['id']}", json={"visibility": "public"}, headers=_headers(self.creator)
        )
        self.assertEqual(allowed_by_owner.status_code, 200, allowed_by_owner.text)
        self.assertEqual(allowed_by_owner.json()["visibility"], "public")

        allowed_by_admin = client.patch(
            f"/api/v1/agents/{agent['id']}", json={"visibility": "team"}, headers=_headers(self.admin)
        )
        self.assertEqual(allowed_by_admin.status_code, 200, allowed_by_admin.text)

        # A non-owner teammate may still edit non-visibility fields.
        renamed = client.patch(
            f"/api/v1/agents/{agent['id']}", json={"name": "Renamed by teammate"}, headers=_headers(self.teammate)
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)


class DefinitionInstallFlowTests(unittest.TestCase):
    """Sprint 8 exit criterion 3: install/copy pins a version-lineaged, independently governed resource."""

    def setUp(self) -> None:
        with SessionLocal.begin() as session:
            self.source_team = make_team(session, name=f"Source {uuid.uuid4()}")
            self.source_owner = make_user(session, display_name="Source Owner")
            make_team_membership(session, self.source_team, self.source_owner, role="owner")

            self.installing_team = make_team(session, name=f"Installer {uuid.uuid4()}")
            self.installer = make_user(session, display_name="Installer")
            make_team_membership(session, self.installing_team, self.installer, role="owner")

            self.private_team = make_team(session, name=f"Private {uuid.uuid4()}")
            self.private_actor = make_user(session, display_name="Private Actor")
            make_team_membership(session, self.private_team, self.private_actor, role="owner")

            for value in (
                self.source_team,
                self.source_owner,
                self.installing_team,
                self.installer,
                self.private_team,
                self.private_actor,
            ):
                session.expunge(value)

    def test_install_agent_pins_source_version_as_independent_resource(self) -> None:
        source = client.post(
            "/api/v1/agents",
            json={"name": "Shared Agent", "visibility": "public"},
            headers=_headers(self.source_owner),
        )
        self.assertEqual(source.status_code, 201, source.text)
        source_agent_id = source.json()["id"]
        version = client.post(
            f"/api/v1/agents/{source_agent_id}/versions",
            json={"instructions": "Do the thing", "capability_manifest": {}},
            headers=_headers(self.source_owner),
        )
        self.assertEqual(version.status_code, 201, version.text)

        installed = client.post(
            f"/api/v1/agents/{source_agent_id}/versions/1/install",
            json={"name": "My Copy"},
            headers=_headers(self.installer),
        )
        self.assertEqual(installed.status_code, 201, installed.text)
        installed_body = installed.json()
        self.assertEqual(installed_body["team_id"], str(self.installing_team.id))
        self.assertEqual(installed_body["visibility"], "private")
        self.assertNotEqual(installed_body["id"], source_agent_id)

        installed_version = client.get(
            f"/api/v1/agents/{installed_body['id']}/versions/1", headers=_headers(self.installer)
        )
        self.assertEqual(installed_version.status_code, 200, installed_version.text)
        self.assertEqual(installed_version.json()["instructions"], "Do the thing")

        lineage = client.get(
            f"/api/v1/agents/{installed_body['id']}/installation", headers=_headers(self.installer)
        )
        self.assertEqual(lineage.status_code, 200, lineage.text)
        self.assertEqual(lineage.json()["installed_by"], str(self.installer.id))

        with SessionLocal() as session:
            record = session.execute(
                select(AgentInstallation).where(
                    AgentInstallation.installed_agent_id == uuid.UUID(installed_body["id"])
                )
            ).scalar_one()
            source_version_row = session.execute(
                select(AgentVersion).where(
                    AgentVersion.agent_id == uuid.UUID(source_agent_id), AgentVersion.version_number == 1
                )
            ).scalar_one()
            self.assertEqual(record.source_agent_version_id, source_version_row.id)

    def test_install_skill_pins_source_version_as_independent_resource(self) -> None:
        source = client.post(
            "/api/v1/skills",
            json={"name": "Shared Skill", "visibility": "team"},
            headers=_headers(self.source_owner),
        )
        self.assertEqual(source.status_code, 201, source.text)
        source_skill_id = source.json()["id"]
        version = client.post(
            f"/api/v1/skills/{source_skill_id}/versions",
            json={"content_ref": "skills/shared/v1", "resource_metadata": {}},
            headers=_headers(self.source_owner),
        )
        self.assertEqual(version.status_code, 201, version.text)

        installed = client.post(
            f"/api/v1/skills/{source_skill_id}/versions/1/install",
            json={},
            headers=_headers(self.installer),
        )
        self.assertEqual(installed.status_code, 201, installed.text)
        installed_body = installed.json()
        self.assertEqual(installed_body["team_id"], str(self.installing_team.id))

        with SessionLocal() as session:
            record = session.execute(
                select(SkillInstallation).where(
                    SkillInstallation.installed_skill_id == uuid.UUID(installed_body["id"])
                )
            ).scalar_one()
            source_version_row = session.execute(
                select(SkillVersion).where(
                    SkillVersion.skill_id == uuid.UUID(source_skill_id), SkillVersion.version_number == 1
                )
            ).scalar_one()
            self.assertEqual(record.source_skill_version_id, source_version_row.id)

    def test_private_source_cannot_be_installed_by_another_team(self) -> None:
        source = client.post(
            "/api/v1/agents",
            json={"name": "Secret Agent", "visibility": "private"},
            headers=_headers(self.source_owner),
        )
        self.assertEqual(source.status_code, 201, source.text)
        source_agent_id = source.json()["id"]
        client.post(
            f"/api/v1/agents/{source_agent_id}/versions",
            json={},
            headers=_headers(self.source_owner),
        )

        denied = client.post(
            f"/api/v1/agents/{source_agent_id}/versions/1/install",
            json={},
            headers=_headers(self.installer),
        )
        self.assertEqual(denied.status_code, 404, denied.text)

    def test_source_owner_cannot_mutate_installed_copy_and_installer_cannot_edit_source(self) -> None:
        source = client.post(
            "/api/v1/agents",
            json={"name": "Shared Agent", "visibility": "public"},
            headers=_headers(self.source_owner),
        )
        source_agent_id = source.json()["id"]
        client.post(f"/api/v1/agents/{source_agent_id}/versions", json={}, headers=_headers(self.source_owner))
        installed = client.post(
            f"/api/v1/agents/{source_agent_id}/versions/1/install", json={}, headers=_headers(self.installer)
        )
        installed_agent_id = installed.json()["id"]

        source_owner_edits_installed = client.patch(
            f"/api/v1/agents/{installed_agent_id}", json={"name": "Reclaimed"}, headers=_headers(self.source_owner)
        )
        self.assertEqual(source_owner_edits_installed.status_code, 404, source_owner_edits_installed.text)

        installer_edits_source = client.patch(
            f"/api/v1/agents/{source_agent_id}", json={"name": "Overwritten"}, headers=_headers(self.installer)
        )
        self.assertEqual(installer_edits_source.status_code, 404, installer_edits_source.text)

    def test_deleting_source_agent_with_installed_derivative_is_rejected(self) -> None:
        source = client.post(
            "/api/v1/agents",
            json={"name": "Shared Agent", "visibility": "public"},
            headers=_headers(self.source_owner),
        )
        source_agent_id = source.json()["id"]
        client.post(f"/api/v1/agents/{source_agent_id}/versions", json={}, headers=_headers(self.source_owner))
        client.post(
            f"/api/v1/agents/{source_agent_id}/versions/1/install", json={}, headers=_headers(self.installer)
        )

        conflict = client.delete(f"/api/v1/agents/{source_agent_id}", headers=_headers(self.source_owner))
        self.assertEqual(conflict.status_code, 409, conflict.text)

    def test_agent_without_dependents_can_be_deleted(self) -> None:
        source = client.post(
            "/api/v1/agents", json={"name": "Disposable Agent"}, headers=_headers(self.source_owner)
        )
        agent_id = source.json()["id"]
        deleted = client.delete(f"/api/v1/agents/{agent_id}", headers=_headers(self.source_owner))
        self.assertEqual(deleted.status_code, 204, deleted.text)
        self.assertEqual(
            client.get(f"/api/v1/agents/{agent_id}", headers=_headers(self.source_owner)).status_code, 404
        )

    def test_installed_copy_is_immune_to_later_source_edits(self) -> None:
        """Sprint 8 exit criterion 3: worker/run snapshots use pinned definitions after source changes."""
        source = client.post(
            "/api/v1/agents",
            json={"name": "Shared Agent", "visibility": "public"},
            headers=_headers(self.source_owner),
        )
        source_agent_id = source.json()["id"]
        client.post(
            f"/api/v1/agents/{source_agent_id}/versions",
            json={"instructions": "Original instructions"},
            headers=_headers(self.source_owner),
        )
        installed = client.post(
            f"/api/v1/agents/{source_agent_id}/versions/1/install", json={}, headers=_headers(self.installer)
        )
        installed_agent_id = installed.json()["id"]

        # The source owner changes visibility and adds a new version after install.
        client.patch(
            f"/api/v1/agents/{source_agent_id}", json={"visibility": "private"}, headers=_headers(self.source_owner)
        )
        client.post(
            f"/api/v1/agents/{source_agent_id}/versions",
            json={"instructions": "Mutated instructions"},
            headers=_headers(self.source_owner),
        )

        pinned = client.get(
            f"/api/v1/agents/{installed_agent_id}/versions/1", headers=_headers(self.installer)
        )
        self.assertEqual(pinned.status_code, 200, pinned.text)
        self.assertEqual(pinned.json()["instructions"], "Original instructions")


if __name__ == "__main__":
    unittest.main()
