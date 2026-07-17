from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from sqlalchemy import create_engine, func, select

from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import SkillVersion
from agentic_os.skill_packages import MAX_RESOURCE_BYTES
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


class SkillPackageApiTests(unittest.TestCase):
    def setUp(self) -> None:
        with SessionLocal.begin() as session:
            team = make_team(session, name=f"Package Team {uuid.uuid4()}")
            self.owner = make_user(session, display_name="Package Owner")
            make_team_membership(session, team, self.owner, role="owner")
            other_team = make_team(session, name=f"Other Team {uuid.uuid4()}")
            self.outsider = make_user(session, display_name="Outsider")
            make_team_membership(session, other_team, self.outsider)
            for value in (self.owner, self.outsider):
                session.expunge(value)
        response = client.post(
            "/api/v1/skills",
            json={"name": "Package Skill", "visibility": "public"},
            headers=_headers(self.owner),
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.skill = response.json()

    def _package(self) -> dict:
        return {
            "manifest": {
                "name": "package-skill",
                "description": "A governed package",
                "resources": ["references/guide.md"],
                "api_key": "manifest-secret",
                "grants": ["must-not-export"],
            },
            "instructions": "Use the supplied guide.",
            "resources": [
                {
                    "path": "references/guide.md",
                    "content": "# Guide\nSafe package content.",
                    "metadata": {"secret_token": "resource-secret", "audience": "agent"},
                }
            ],
            "declared_capabilities": ["research", "summarize"],
            "provenance": {
                "source": "imported",
                "repository": "https://example.test/package",
                "access_token": "provenance-secret",
            },
        }

    def test_author_package_persists_hashes_validation_and_redacted_export(self) -> None:
        created = client.post(
            f"/api/v1/skills/{self.skill['id']}/versions",
            json=self._package(),
            headers=_headers(self.owner),
        )
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertEqual(body["validation_status"], "valid")
        self.assertEqual(body["validation_diagnostics"], [])
        self.assertEqual(len(body["package_hash"]), 64)
        self.assertEqual(len(body["resources"][0]["sha256"]), 64)
        self.assertEqual(body["content_ref"], f"sha256:{body['package_hash']}")
        self.assertNotIn("api_key", body["manifest"])
        self.assertNotIn("grants", body["manifest"])
        self.assertNotIn("secret_token", body["resources"][0]["metadata"])
        self.assertNotIn("access_token", body["provenance"])
        self.assertNotIn("manifest-secret", created.text)
        self.assertNotIn("resource-secret", created.text)
        self.assertNotIn("provenance-secret", created.text)

        exported = client.get(
            f"/api/v1/skills/{self.skill['id']}/versions/1/export",
            headers=_headers(self.outsider),
        )
        self.assertEqual(exported.status_code, 200, exported.text)
        export = exported.json()
        self.assertEqual(export["format_version"], 1)
        self.assertNotIn("api_key", export["manifest"])
        self.assertNotIn("grants", export["manifest"])
        self.assertNotIn("secret_token", export["resources"][0]["metadata"])
        self.assertNotIn("access_token", export["provenance"])
        self.assertNotIn("created_by", export)
        self.assertNotIn("team_id", export)
        self.assertNotIn("grant", exported.text.lower())
        self.assertNotIn("manifest-secret", exported.text)
        self.assertNotIn("resource-secret", exported.text)
        self.assertNotIn("provenance-secret", exported.text)

    def test_invalid_package_returns_diagnostics_without_persisting_version(self) -> None:
        package = self._package()
        package["manifest"]["name"] = ""
        package["manifest"]["resources"] = ["../escape.md", "missing.md"]
        package["resources"].append({"path": "../escape.md", "content": "unsafe"})
        package["declared_capabilities"] = ["research", "research"]

        rejected = client.post(
            f"/api/v1/skills/{self.skill['id']}/versions",
            json=package,
            headers=_headers(self.owner),
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)
        detail = rejected.json()["detail"]
        self.assertEqual(detail["code"], "invalid_skill_package")
        codes = {item["code"] for item in detail["diagnostics"]}
        self.assertTrue(
            {
                "required_manifest_field",
                "unsafe_resource_path",
                "duplicate_capability",
                "malformed_resource_reference",
                "missing_resource_reference",
            }
            <= codes
        )
        with SessionLocal() as session:
            count = session.execute(
                select(func.count(SkillVersion.id)).where(
                    SkillVersion.skill_id == uuid.UUID(self.skill["id"])
                )
            ).scalar_one()
        self.assertEqual(count, 0)

    def test_package_size_bounds_and_ownership_are_enforced(self) -> None:
        oversized = self._package()
        oversized["resources"][0]["content"] = "x" * (MAX_RESOURCE_BYTES + 1)
        rejected = client.post(
            f"/api/v1/skills/{self.skill['id']}/versions",
            json=oversized,
            headers=_headers(self.owner),
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertIn(
            "resource_too_large",
            {item["code"] for item in rejected.json()["detail"]["diagnostics"]},
        )

        denied = client.post(
            f"/api/v1/skills/{self.skill['id']}/versions",
            json=self._package(),
            headers=_headers(self.outsider),
        )
        self.assertEqual(denied.status_code, 404, denied.text)

    def test_versions_are_immutable_and_install_copies_complete_package(self) -> None:
        first = client.post(
            f"/api/v1/skills/{self.skill['id']}/versions",
            json=self._package(),
            headers=_headers(self.owner),
        )
        self.assertEqual(first.status_code, 201, first.text)
        changed = self._package()
        changed["instructions"] = "Updated instructions create a new version."
        second = client.post(
            f"/api/v1/skills/{self.skill['id']}/versions",
            json=changed,
            headers=_headers(self.owner),
        )
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(second.json()["version_number"], 2)
        self.assertNotEqual(first.json()["package_hash"], second.json()["package_hash"])
        self.assertEqual(
            client.patch(
                f"/api/v1/skills/{self.skill['id']}/versions/1",
                json={"instructions": "mutate"},
                headers=_headers(self.owner),
            ).status_code,
            405,
        )
        original = client.get(
            f"/api/v1/skills/{self.skill['id']}/versions/1",
            headers=_headers(self.owner),
        ).json()
        self.assertEqual(original["instructions"], "Use the supplied guide.")

        installed = client.post(
            f"/api/v1/skills/{self.skill['id']}/versions/1/install",
            json={},
            headers=_headers(self.outsider),
        )
        self.assertEqual(installed.status_code, 201, installed.text)
        installed_version = client.get(
            f"/api/v1/skills/{installed.json()['id']}/versions/1",
            headers=_headers(self.outsider),
        )
        self.assertEqual(installed_version.status_code, 200, installed_version.text)
        copied = installed_version.json()
        self.assertEqual(copied["manifest"], original["manifest"])
        self.assertEqual(copied["resources"], original["resources"])
        self.assertEqual(copied["declared_capabilities"], original["declared_capabilities"])
        self.assertEqual(copied["provenance"], original["provenance"])
        self.assertEqual(copied["package_hash"], original["package_hash"])

    def test_legacy_content_reference_flow_remains_supported(self) -> None:
        created = client.post(
            f"/api/v1/skills/{self.skill['id']}/versions",
            json={"content_ref": "skills/legacy/v1", "resource_metadata": {"format": "markdown"}},
            headers=_headers(self.owner),
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["validation_status"], "legacy")
        self.assertIsNone(created.json()["package_hash"])
