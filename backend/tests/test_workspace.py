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
from agentic_os.domain.models import (
    Agent,
    AgentVersion,
    AuditEvent,
    Goal,
    Project,
    Run,
    Task,
    Team,
    User,
    WorkspacePromotion,
    WorkspaceResource,
    WorkspaceResourceLease,
)
from agentic_os.worker import claim_ready_task
from agentic_os.worker.workspace import (
    InvalidResourceKeyError,
    WorkspaceConflictError,
    WorkspaceLeaseLostError,
    canonical_resource_key,
    promote_workspace_changes,
)

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
    global TEST_DATABASE_URL
    TEST_DATABASE_URL = os.environ.get("AGENTIC_OS_DATABASE_URL", database_url())
    try:
        probe = create_database_engine(TEST_DATABASE_URL)
        with probe.connect():
            pass
        probe.dispose()
    except Exception as error:  # pragma: no cover - environment guard
        raise unittest.SkipTest(f"PostgreSQL is not reachable: {error}")
    _apply_migrations_from_zero(TEST_DATABASE_URL)


class ResourceKeyTests(unittest.TestCase):
    def test_canonical_project_relative_keys(self) -> None:
        self.assertEqual(canonical_resource_key("docs/report.md"), "docs/report.md")
        for invalid in ("/etc/passwd", "docs/../secret", "docs//report.md", "docs\\report.md", "./docs"):
            with self.subTest(invalid=invalid), self.assertRaises(InvalidResourceKeyError):
                canonical_resource_key(invalid)


class WorkspacePromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_database_engine(TEST_DATABASE_URL)
        self.Session = session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _build_task(self, session, keys: tuple[str, ...]) -> Task:
        team = Team(name=f"Workspace Team {uuid.uuid4()}")
        user = User(email=f"workspace-{uuid.uuid4()}@example.test", display_name="Operator")
        session.add_all([team, user])
        session.flush()
        project = Project(team_id=team.id, created_by=user.id, name="Workspace Project")
        agent = Agent(team_id=team.id, created_by=user.id, name="Workspace Agent")
        session.add_all([project, agent])
        session.flush()
        agent_version = AgentVersion(agent_id=agent.id, version_number=1, capability_manifest={})
        goal = Goal(project_id=project.id, created_by=user.id, title="Workspace Goal", status="active")
        session.add_all([agent_version, goal])
        session.flush()
        task = Task(
            goal_id=goal.id,
            title="Mutate workspace",
            status="ready",
            assigned_agent_version_id=agent_version.id,
            assignment_status="assigned",
            resource_intent=[{"resource_key": key, "intent": "write"} for key in keys],
        )
        session.add(task)
        session.commit()
        return task

    def _claim_with_run(self, session, task_id: uuid.UUID, worker_id: str = "workspace-worker"):
        task = claim_ready_task(session, worker_id)
        self.assertIsNotNone(task)
        self.assertEqual(task.id, task_id)
        run = Run(
            task_id=task.id,
            attempt_number=1,
            idempotency_key=f"{task.id}:1",
            lease_token=task.lease_token,
            agent_version_id=task.assigned_agent_version_id,
            status="running",
        )
        session.add(run)
        session.flush()
        return task, run

    def test_disjoint_resources_promote_atomically(self) -> None:
        with self.Session() as session:
            original = self._build_task(session, ("docs/a.md", "docs/b.md"))
            task, run = self._claim_with_run(session, original.id)
            promotion = promote_workspace_changes(session, task, run, "workspace-worker")
            session.commit()

            self.assertEqual(promotion.status, "promoted")
            self.assertEqual(promotion.expected_revisions, {"docs/a.md": 0, "docs/b.md": 0})
            self.assertEqual(promotion.resulting_revisions, {"docs/a.md": 1, "docs/b.md": 1})
            resources = list(
                session.execute(
                    select(WorkspaceResource).where(WorkspaceResource.project_id == promotion.project_id)
                ).scalars()
            )
            self.assertEqual({row.resource_key: row.revision for row in resources}, {"docs/a.md": 1, "docs/b.md": 1})
            self.assertTrue(
                session.execute(
                    select(AuditEvent).where(
                        AuditEvent.run_id == run.id,
                        AuditEvent.event_type == "workspace.promoted",
                    )
                ).scalar_one()
            )

    def test_changed_expected_revision_persists_explicit_conflict(self) -> None:
        with self.Session() as session:
            original = self._build_task(session, ("shared/output.md",))
            task, run = self._claim_with_run(session, original.id)
            resource = session.execute(select(WorkspaceResource)).scalar_one()
            resource.revision = 1
            with self.assertRaises(WorkspaceConflictError):
                promote_workspace_changes(session, task, run, "workspace-worker")
            session.commit()

            promotion = session.execute(
                select(WorkspacePromotion).where(WorkspacePromotion.run_id == run.id)
            ).scalar_one()
            self.assertEqual(promotion.status, "conflict")
            self.assertEqual(
                promotion.conflict_details["shared/output.md"],
                {"expected_revision": 0, "actual_revision": 1},
            )

    def test_stale_fencing_token_cannot_promote(self) -> None:
        with self.Session() as session:
            original = self._build_task(session, ("shared/output.md",))
            task, run = self._claim_with_run(session, original.id)
            lease = session.execute(
                select(WorkspaceResourceLease).where(WorkspaceResourceLease.task_id == task.id)
            ).scalar_one()
            resource = session.get(WorkspaceResource, lease.resource_id)
            resource.last_fencing_token += 1
            with self.assertRaises(WorkspaceLeaseLostError):
                promote_workspace_changes(session, task, run, "workspace-worker")
            session.commit()

            promotion = session.execute(
                select(WorkspacePromotion).where(WorkspacePromotion.run_id == run.id)
            ).scalar_one()
            self.assertEqual(promotion.status, "denied")
            self.assertIn("shared/output.md", promotion.conflict_details)


if __name__ == "__main__":
    unittest.main()
