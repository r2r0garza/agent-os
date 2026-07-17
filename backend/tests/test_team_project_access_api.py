from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from sqlalchemy import create_engine

from agentic_os.domain import create_database_engine, database_url, session_factory
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


class TeamAndProjectAccessApiTests(unittest.TestCase):
    def setUp(self) -> None:
        with SessionLocal.begin() as session:
            self.team = make_team(session, name=f"Owner team {uuid.uuid4()}")
            self.owner = make_user(session, display_name="Owner")
            make_team_membership(session, self.team, self.owner, role="owner")
            self.teammate = make_user(session, display_name="Teammate")
            make_team_membership(session, self.team, self.teammate)

            other_team = make_team(session, name=f"Other team {uuid.uuid4()}")
            self.outsider = make_user(session, display_name="Outsider")
            make_team_membership(session, other_team, self.outsider)
            self.admin = make_user(session, display_name="Admin", role="admin")

            self.project = make_project(session, self.team, self.owner, name="Shared project")

            for value in (
                self.team,
                self.owner,
                self.teammate,
                other_team,
                self.outsider,
                self.admin,
                self.project,
            ):
                session.expunge(value)

    @staticmethod
    def _headers(actor) -> dict[str, str]:
        return {"X-Agentic-User-ID": str(actor.id)}

    def test_list_teams_scoped_to_membership_and_admin_sees_all(self) -> None:
        member_view = client.get("/api/v1/teams", headers=self._headers(self.owner))
        self.assertEqual(member_view.status_code, 200, member_view.text)
        self.assertEqual([item["id"] for item in member_view.json()], [str(self.team.id)])

        admin_view = client.get("/api/v1/teams", headers=self._headers(self.admin))
        self.assertEqual(admin_view.status_code, 200, admin_view.text)
        team_ids = {item["id"] for item in admin_view.json()}
        self.assertIn(str(self.team.id), team_ids)

    def test_list_team_memberships_requires_team_access(self) -> None:
        allowed = client.get(
            f"/api/v1/teams/{self.team.id}/memberships", headers=self._headers(self.owner)
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        emails = {item["user_email"] for item in allowed.json()}
        self.assertIn(self.owner.email, emails)
        self.assertIn(self.teammate.email, emails)

        denied = client.get(
            f"/api/v1/teams/{self.team.id}/memberships", headers=self._headers(self.outsider)
        )
        self.assertEqual(denied.status_code, 404)

        admin_allowed = client.get(
            f"/api/v1/teams/{self.team.id}/memberships", headers=self._headers(self.admin)
        )
        self.assertEqual(admin_allowed.status_code, 200, admin_allowed.text)

    def test_list_users_requires_admin(self) -> None:
        denied = client.get("/api/v1/users", headers=self._headers(self.owner))
        self.assertEqual(denied.status_code, 403)

        allowed = client.get("/api/v1/users", headers=self._headers(self.admin))
        self.assertEqual(allowed.status_code, 200, allowed.text)
        emails = {item["email"] for item in allowed.json()}
        self.assertIn(self.owner.email, emails)

    def test_grant_project_member_requires_owner_or_admin(self) -> None:
        # A user with no project access at all cannot grant access to anyone;
        # the read-access check fails closed with 404 before ownership is checked.
        unauthorized = client.post(
            f"/api/v1/projects/{self.project.id}/members",
            json={"user_id": str(self.teammate.id)},
            headers=self._headers(self.teammate),
        )
        self.assertEqual(unauthorized.status_code, 404, unauthorized.text)

        with SessionLocal.begin() as session:
            second_teammate = make_user(session, display_name="Second teammate")
            make_team_membership(session, self.team, second_teammate)
            make_project_member(session, self.project, self.teammate, granted_by=self.owner)
            session.expunge(second_teammate)

        # A granted (non-owner, non-admin) member has project access but still
        # cannot grant access to someone else.
        forbidden = client.post(
            f"/api/v1/projects/{self.project.id}/members",
            json={"user_id": str(second_teammate.id)},
            headers=self._headers(self.teammate),
        )
        self.assertEqual(forbidden.status_code, 403, forbidden.text)

    def test_grant_project_member_requires_team_membership(self) -> None:
        rejected = client.post(
            f"/api/v1/projects/{self.project.id}/members",
            json={"user_id": str(self.outsider.id)},
            headers=self._headers(self.owner),
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)

    def test_grant_and_revoke_project_member_updates_access(self) -> None:
        before = client.get(
            f"/api/v1/projects/{self.project.id}", headers=self._headers(self.teammate)
        )
        self.assertEqual(before.status_code, 404)

        granted = client.post(
            f"/api/v1/projects/{self.project.id}/members",
            json={"user_id": str(self.teammate.id)},
            headers=self._headers(self.owner),
        )
        self.assertEqual(granted.status_code, 201, granted.text)
        self.assertEqual(granted.json()["user_id"], str(self.teammate.id))
        self.assertEqual(granted.json()["granted_by"], str(self.owner.id))

        duplicate = client.post(
            f"/api/v1/projects/{self.project.id}/members",
            json={"user_id": str(self.teammate.id)},
            headers=self._headers(self.owner),
        )
        self.assertEqual(duplicate.status_code, 409)

        after_grant = client.get(
            f"/api/v1/projects/{self.project.id}", headers=self._headers(self.teammate)
        )
        self.assertEqual(after_grant.status_code, 200)

        listed = client.get(
            f"/api/v1/projects/{self.project.id}/members", headers=self._headers(self.teammate)
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([item["user_id"] for item in listed.json()], [str(self.teammate.id)])

        revoke_denied = client.delete(
            f"/api/v1/projects/{self.project.id}/members/{self.teammate.id}",
            headers=self._headers(self.teammate),
        )
        self.assertEqual(revoke_denied.status_code, 403)

        revoked = client.delete(
            f"/api/v1/projects/{self.project.id}/members/{self.teammate.id}",
            headers=self._headers(self.owner),
        )
        self.assertEqual(revoked.status_code, 204, revoked.text)

        after_revoke = client.get(
            f"/api/v1/projects/{self.project.id}", headers=self._headers(self.teammate)
        )
        self.assertEqual(after_revoke.status_code, 404)

        missing = client.delete(
            f"/api/v1/projects/{self.project.id}/members/{self.teammate.id}",
            headers=self._headers(self.owner),
        )
        self.assertEqual(missing.status_code, 404)

    def test_admin_can_grant_on_behalf_of_project_owner(self) -> None:
        granted = client.post(
            f"/api/v1/projects/{self.project.id}/members",
            json={"user_id": str(self.teammate.id)},
            headers=self._headers(self.admin),
        )
        self.assertEqual(granted.status_code, 201, granted.text)


if __name__ == "__main__":
    unittest.main()
