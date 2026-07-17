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
from sqlalchemy.exc import IntegrityError

from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import (
    Agent,
    AgentInstallation,
    AgentVersion,
    Project,
    ProjectMember,
    Skill,
    SkillInstallation,
    SkillVersion,
    Task,
    Team,
    TeamMembership,
    User,
)

from factories import (
    make_project,
    make_project_member,
    make_team,
    make_team_membership,
    make_team_with_members,
    make_user,
)

BACKEND_ROOT = Path(__file__).parents[1]


def setUpModule() -> None:
    global TEST_DATABASE_URL
    TEST_DATABASE_URL = os.environ.get("AGENTIC_OS_DATABASE_URL", database_url())
    try:
        probe = create_database_engine(TEST_DATABASE_URL)
        with probe.connect():
            pass
        probe.dispose()
    except Exception as error:  # pragma: no cover - environment guard
        raise unittest.SkipTest(
            f"PostgreSQL is not reachable at {TEST_DATABASE_URL!r}; set "
            "AGENTIC_OS_DATABASE_URL to run team access persistence tests: "
            f"{error}"
        )

    engine = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    engine.dispose()
    subprocess.run(
        [str(BACKEND_ROOT / ".venv" / "bin" / "alembic"), "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=dict(os.environ, AGENTIC_OS_DATABASE_URL=TEST_DATABASE_URL),
        check=True,
        capture_output=True,
        text=True,
    )


class TeamMembershipRoleTests(unittest.TestCase):
    """Sprint 8 exit criterion 1: team membership carries a role."""

    def setUp(self) -> None:
        self.engine = create_database_engine(TEST_DATABASE_URL)
        self.Session = session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_membership_defaults_to_member_role(self) -> None:
        with self.Session() as session:
            team = make_team(session)
            user = make_user(session)
            membership = TeamMembership(team_id=team.id, user_id=user.id)
            session.add(membership)
            session.commit()
            session.refresh(membership)
            self.assertEqual(membership.role, "member")

    def test_membership_supports_owner_role(self) -> None:
        with self.Session() as session:
            team, owner, members = make_team_with_members(session, member_emails=["a@example.test"])
            session.commit()

            owner_membership = session.execute(
                select(TeamMembership).where(
                    TeamMembership.team_id == team.id, TeamMembership.user_id == owner.id
                )
            ).scalar_one()
            member_membership = session.execute(
                select(TeamMembership).where(
                    TeamMembership.team_id == team.id, TeamMembership.user_id == members[0].id
                )
            ).scalar_one()

            self.assertEqual(owner_membership.role, "owner")
            self.assertEqual(member_membership.role, "member")

    def test_team_membership_uniqueness_still_enforced_with_role_column(self) -> None:
        with self.Session() as session:
            team = make_team(session)
            user = make_user(session)
            session.add(TeamMembership(team_id=team.id, user_id=user.id, role="owner"))
            session.commit()

            session.add(TeamMembership(team_id=team.id, user_id=user.id, role="member"))
            with self.assertRaises(IntegrityError):
                session.commit()
            session.rollback()


class ProjectGrantAttributionTests(unittest.TestCase):
    """Sprint 8 exit criterion 1: project access grants retain attribution."""

    def setUp(self) -> None:
        self.engine = create_database_engine(TEST_DATABASE_URL)
        self.Session = session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_project_member_records_who_granted_access(self) -> None:
        with self.Session() as session:
            team, owner, members = make_team_with_members(session, member_emails=["grantee@example.test"])
            project = make_project(session, team, owner, name="Shared Project")
            grant = make_project_member(session, project, members[0], granted_by=owner)
            session.commit()
            session.refresh(grant)

            self.assertEqual(grant.granted_by, owner.id)

    def test_project_member_grant_survives_granter_deletion(self) -> None:
        with self.Session() as session:
            team, owner, members = make_team_with_members(session, member_emails=["grantee2@example.test"])
            project = make_project(session, team, owner, name="Durable Grant Project")
            grantor = make_user(session, display_name="Temporary Admin", role="admin")
            grant = make_project_member(session, project, members[0], granted_by=grantor)
            session.commit()
            grant_id = grant.id

            session.delete(grantor)
            session.commit()
            session.expire_all()

            surviving_grant = session.get(ProjectMember, grant_id)
            self.assertIsNotNone(surviving_grant)
            self.assertIsNone(surviving_grant.granted_by)


class TaskAttributionTests(unittest.TestCase):
    """Sprint 8 exit criterion 1: tasks retain creator/actor attribution."""

    def setUp(self) -> None:
        self.engine = create_database_engine(TEST_DATABASE_URL)
        self.Session = session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_task_records_creator(self) -> None:
        from agentic_os.domain.models import Goal

        with self.Session() as session:
            team = make_team(session)
            user = make_user(session)
            project = make_project(session, team, user)
            goal = Goal(project_id=project.id, created_by=user.id, title="Goal")
            session.add(goal)
            session.flush()
            task = Task(goal_id=goal.id, created_by=user.id, title="Task with attribution")
            session.add(task)
            session.commit()
            session.refresh(task)

            self.assertEqual(task.created_by, user.id)

    def test_task_creator_is_optional_for_decomposition_generated_tasks(self) -> None:
        from agentic_os.domain.models import Goal

        with self.Session() as session:
            team = make_team(session)
            user = make_user(session)
            project = make_project(session, team, user)
            goal = Goal(project_id=project.id, created_by=user.id, title="Goal")
            session.add(goal)
            session.flush()
            task = Task(goal_id=goal.id, title="System decomposed task")
            session.add(task)
            session.commit()
            session.refresh(task)

            self.assertIsNone(task.created_by)


class InstalledDefinitionLineageTests(unittest.TestCase):
    """Sprint 8 exit criterion 1: installed agent/skill definitions preserve lineage."""

    def setUp(self) -> None:
        self.engine = create_database_engine(TEST_DATABASE_URL)
        self.Session = session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_agent_installation_pins_source_version_and_installer(self) -> None:
        with self.Session() as session:
            source_team = make_team(session, name="Source Team")
            source_user = make_user(session)
            source_agent = Agent(
                team_id=source_team.id, created_by=source_user.id, name="Public Agent", visibility="public"
            )
            session.add(source_agent)
            session.flush()
            source_version = AgentVersion(agent_id=source_agent.id, version_number=1)
            session.add(source_version)
            session.flush()

            installing_team = make_team(session, name="Installing Team")
            installer = make_user(session, display_name="Installer")
            installed_agent = Agent(
                team_id=installing_team.id,
                created_by=installer.id,
                name="Public Agent (installed)",
                visibility="private",
            )
            session.add(installed_agent)
            session.flush()
            installation = AgentInstallation(
                installed_agent_id=installed_agent.id,
                source_agent_version_id=source_version.id,
                installed_by=installer.id,
            )
            session.add(installation)
            session.commit()
            session.refresh(installation)

            self.assertEqual(installation.source_agent_version_id, source_version.id)
            self.assertEqual(installation.installed_by, installer.id)
            self.assertEqual(installation.installed_agent_id, installed_agent.id)

    def test_agent_installation_is_one_to_one_with_installed_agent(self) -> None:
        with self.Session() as session:
            team = make_team(session)
            user = make_user(session)
            source_agent = Agent(team_id=team.id, created_by=user.id, name="Source", visibility="public")
            session.add(source_agent)
            session.flush()
            version_one = AgentVersion(agent_id=source_agent.id, version_number=1)
            version_two = AgentVersion(agent_id=source_agent.id, version_number=2)
            session.add_all([version_one, version_two])
            session.flush()

            installed_agent = Agent(team_id=team.id, created_by=user.id, name="Installed", visibility="private")
            session.add(installed_agent)
            session.flush()
            session.add(
                AgentInstallation(
                    installed_agent_id=installed_agent.id,
                    source_agent_version_id=version_one.id,
                    installed_by=user.id,
                )
            )
            session.commit()

            session.add(
                AgentInstallation(
                    installed_agent_id=installed_agent.id,
                    source_agent_version_id=version_two.id,
                    installed_by=user.id,
                )
            )
            with self.assertRaises(IntegrityError):
                session.commit()
            session.rollback()

    def test_skill_installation_pins_source_version_and_installer(self) -> None:
        with self.Session() as session:
            source_team = make_team(session, name="Skill Source Team")
            source_user = make_user(session)
            source_skill = Skill(
                team_id=source_team.id, created_by=source_user.id, name="Public Skill", visibility="team"
            )
            session.add(source_skill)
            session.flush()
            source_version = SkillVersion(
                skill_id=source_skill.id, version_number=1, content_ref="skills/public-skill/v1"
            )
            session.add(source_version)
            session.flush()

            installing_team = make_team(session, name="Skill Installing Team")
            installer = make_user(session, display_name="Skill Installer")
            installed_skill = Skill(
                team_id=installing_team.id,
                created_by=installer.id,
                name="Public Skill (installed)",
                visibility="private",
            )
            session.add(installed_skill)
            session.flush()
            installation = SkillInstallation(
                installed_skill_id=installed_skill.id,
                source_skill_version_id=source_version.id,
                installed_by=installer.id,
            )
            session.add(installation)
            session.commit()
            session.refresh(installation)

            self.assertEqual(installation.source_skill_version_id, source_version.id)
            self.assertEqual(installation.installed_by, installer.id)

    def test_installation_records_carry_no_secret_material(self) -> None:
        agent_columns = {column.name for column in AgentInstallation.__table__.columns}
        skill_columns = {column.name for column in SkillInstallation.__table__.columns}
        forbidden = {"credential", "secret", "ciphertext", "api_key", "password", "token"}
        for column_name in agent_columns | skill_columns:
            lowered = column_name.lower()
            self.assertFalse(
                any(term in lowered for term in forbidden),
                f"unexpected secret-shaped column on installation lineage table: {column_name}",
            )


class DefaultOperatorMembershipBackfillTests(unittest.TestCase):
    """Sprint 8 exit criterion 1: the local single-operator path is explicit,
    not a hidden singleton assumption, once bootstrap runs."""

    def setUp(self) -> None:
        self.engine = create_database_engine(TEST_DATABASE_URL)
        self.Session = session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_ensure_default_team_membership_creates_explicit_owner_link(self) -> None:
        from agentic_os.api.bootstrap import ensure_default_team_membership

        with self.Session() as session:
            team, user = ensure_default_team_membership(session)
            session.commit()

            membership = session.execute(
                select(TeamMembership).where(
                    TeamMembership.team_id == team.id, TeamMembership.user_id == user.id
                )
            ).scalar_one()
            self.assertEqual(membership.role, "owner")

    def test_ensure_default_team_membership_is_idempotent(self) -> None:
        from agentic_os.api.bootstrap import ensure_default_team_membership

        with self.Session() as session:
            team, user = ensure_default_team_membership(session)
            session.commit()
            ensure_default_team_membership(session)
            session.commit()

            rows = session.execute(
                select(TeamMembership.id).where(
                    TeamMembership.team_id == team.id, TeamMembership.user_id == user.id
                )
            ).all()
            self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
