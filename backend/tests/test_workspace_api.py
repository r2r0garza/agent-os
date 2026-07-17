from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sqlalchemy import create_engine, select

from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import (
    Agent,
    AgentVersion,
    Goal,
    Project,
    ProjectMember,
    Run,
    Task,
    Team,
    TeamMembership,
    User,
    WorkspacePromotion,
    WorkspaceResource,
    WorkspaceResourceLease,
)

BACKEND_ROOT = Path(__file__).parents[1]


def setUpModule() -> None:
    global client, SessionLocal
    from agentic_os.api.deps import _engine

    if _engine.cache_info().currsize:
        _engine().dispose()
        _engine.cache_clear()
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
        ["alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=dict(os.environ, AGENTIC_OS_DATABASE_URL=db_url),
        check=True,
        capture_output=True,
        text=True,
    )
    os.environ["AGENTIC_OS_DATABASE_URL"] = db_url

    from fastapi.testclient import TestClient

    from agentic_os.api.app import create_app

    client = TestClient(create_app())
    client.get("/api/v1/projects")
    SessionLocal = session_factory(create_database_engine(db_url))


class WorkspaceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        now = datetime.now(timezone.utc)
        with SessionLocal.begin() as session:
            admin = session.execute(
                select(User).where(User.email == "operator@local")
            ).scalar_one()
            team = session.execute(
                select(Team).where(Team.name == "Default Team")
            ).scalar_one()
            member = User(
                email=f"workspace-member-{uuid.uuid4()}@example.test",
                display_name="Workspace Member",
            )
            outsider = User(
                email=f"workspace-outsider-{uuid.uuid4()}@example.test",
                display_name="Workspace Outsider",
            )
            session.add_all([member, outsider])
            session.flush()
            session.add_all(
                [
                    TeamMembership(team_id=team.id, user_id=member.id),
                    TeamMembership(team_id=team.id, user_id=outsider.id),
                ]
            )
            project = Project(
                team_id=team.id,
                created_by=admin.id,
                name=f"Workspace Evidence {uuid.uuid4()}",
            )
            other_project = Project(
                team_id=team.id,
                created_by=admin.id,
                name=f"Other Workspace Evidence {uuid.uuid4()}",
            )
            agent = Agent(
                team_id=team.id,
                created_by=admin.id,
                name=f"Workspace API Agent {uuid.uuid4()}",
            )
            session.add_all([project, other_project, agent])
            session.flush()
            session.add(ProjectMember(project_id=project.id, user_id=member.id))
            agent_version = AgentVersion(
                agent_id=agent.id,
                version_number=1,
                capability_manifest={},
            )
            session.add(agent_version)
            session.flush()

            self.project_id = project.id
            self.other_project_id = other_project.id
            self.admin_id = admin.id
            self.member_id = member.id
            self.outsider_id = outsider.id

            goal = Goal(
                project_id=project.id,
                created_by=admin.id,
                title="Inspect workspace evidence",
                status="active",
            )
            other_goal = Goal(
                project_id=other_project.id,
                created_by=admin.id,
                title="Other workspace evidence",
                status="active",
            )
            session.add_all([goal, other_goal])
            session.flush()

            active_task, active_run = self._task_and_run(
                session,
                goal,
                agent_version,
                title="Active lease",
                lease_owner="worker-active",
                lease_token=11,
                expires_at=now + timedelta(minutes=5),
            )
            stale_task, stale_run = self._task_and_run(
                session,
                goal,
                agent_version,
                title="Stale lease",
                lease_owner="worker-stale",
                lease_token=12,
                expires_at=now - timedelta(minutes=5),
            )
            fenced_task, fenced_run = self._task_and_run(
                session,
                goal,
                agent_version,
                title="Fenced lease",
                lease_owner="worker-fenced",
                lease_token=13,
                expires_at=now + timedelta(minutes=5),
            )
            conflict_task, conflict_run = self._task_and_run(
                session,
                goal,
                agent_version,
                title="Conflicted promotion",
                lease_owner=None,
                lease_token=14,
                expires_at=None,
            )
            promoted_task, promoted_run = self._task_and_run(
                session,
                goal,
                agent_version,
                title="Successful promotion",
                lease_owner=None,
                lease_token=15,
                expires_at=None,
            )
            other_task, other_run = self._task_and_run(
                session,
                other_goal,
                agent_version,
                title="Other project lease",
                lease_owner="worker-other",
                lease_token=16,
                expires_at=now + timedelta(minutes=5),
            )

            self._lease(
                session,
                project,
                active_task,
                resource_key="docs/active.md",
                revision=2,
                last_fencing_token=4,
                fencing_token=4,
                expected_revision=2,
                expires_at=active_task.lease_expires_at,
            )
            self._lease(
                session,
                project,
                stale_task,
                resource_key="docs/stale.md",
                revision=5,
                last_fencing_token=6,
                fencing_token=6,
                expected_revision=5,
                expires_at=stale_task.lease_expires_at,
            )
            self._lease(
                session,
                project,
                fenced_task,
                resource_key="docs/fenced.md",
                revision=8,
                last_fencing_token=10,
                fencing_token=9,
                expected_revision=8,
                expires_at=fenced_task.lease_expires_at,
            )
            self._lease(
                session,
                other_project,
                other_task,
                resource_key="docs/other.md",
                revision=1,
                last_fencing_token=2,
                fencing_token=2,
                expected_revision=1,
                expires_at=other_task.lease_expires_at,
            )
            session.add_all(
                [
                    WorkspacePromotion(
                        project_id=project.id,
                        task_id=conflict_task.id,
                        run_id=conflict_run.id,
                        status="conflict",
                        expected_revisions={"shared/output.md": 3},
                        conflict_details={
                            "shared/output.md": {
                                "expected_revision": 3,
                                "actual_revision": 4,
                            }
                        },
                    ),
                    WorkspacePromotion(
                        project_id=project.id,
                        task_id=promoted_task.id,
                        run_id=promoted_run.id,
                        status="promoted",
                        expected_revisions={"docs/report.md": 4},
                        resulting_revisions={"docs/report.md": 5},
                    ),
                    WorkspacePromotion(
                        project_id=other_project.id,
                        task_id=other_task.id,
                        run_id=other_run.id,
                        status="conflict",
                        expected_revisions={"other/shared.md": 0},
                        conflict_details={
                            "other/shared.md": {
                                "expected_revision": 0,
                                "actual_revision": 1,
                            }
                        },
                    ),
                ]
            )

            self.active_run_id = active_run.id
            self.stale_run_id = stale_run.id
            self.fenced_run_id = fenced_run.id
            self.conflict_run_id = conflict_run.id
            self.promoted_run_id = promoted_run.id

    @staticmethod
    def _task_and_run(
        session,
        goal: Goal,
        agent_version: AgentVersion,
        *,
        title: str,
        lease_owner: str | None,
        lease_token: int,
        expires_at: datetime | None,
    ) -> tuple[Task, Run]:
        task = Task(
            goal_id=goal.id,
            title=title,
            status="running",
            assigned_agent_version_id=agent_version.id,
            assignment_status="assigned",
            lease_owner=lease_owner,
            lease_token=lease_token,
            lease_expires_at=expires_at,
        )
        session.add(task)
        session.flush()
        run = Run(
            task_id=task.id,
            attempt_number=1,
            idempotency_key=f"workspace-api-{task.id}",
            lease_token=lease_token,
            agent_version_id=agent_version.id,
            status="running",
        )
        session.add(run)
        session.flush()
        return task, run

    @staticmethod
    def _lease(
        session,
        project: Project,
        task: Task,
        *,
        resource_key: str,
        revision: int,
        last_fencing_token: int,
        fencing_token: int,
        expected_revision: int,
        expires_at: datetime,
    ) -> None:
        resource = WorkspaceResource(
            project_id=project.id,
            resource_key=resource_key,
            revision=revision,
            last_fencing_token=last_fencing_token,
        )
        session.add(resource)
        session.flush()
        session.add(
            WorkspaceResourceLease(
                resource_id=resource.id,
                task_id=task.id,
                lease_owner=task.lease_owner,
                task_lease_token=task.lease_token,
                fencing_token=fencing_token,
                expected_revision=expected_revision,
                expires_at=expires_at,
            )
        )

    @staticmethod
    def _headers(user_id: uuid.UUID) -> dict[str, str]:
        return {"X-Agentic-User-ID": str(user_id)}

    def test_project_member_lists_active_stale_and_fenced_leases(self) -> None:
        response = client.get(
            f"/api/v1/projects/{self.project_id}/workspace/leases",
            headers=self._headers(self.member_id),
        )
        self.assertEqual(response.status_code, 200, response.text)
        leases = {item["resource_key"]: item for item in response.json()}
        self.assertEqual(set(leases), {"docs/active.md", "docs/stale.md", "docs/fenced.md"})
        self.assertEqual(leases["docs/active.md"]["state"], "active")
        self.assertEqual(leases["docs/active.md"]["owner"], "worker-active")
        self.assertEqual(leases["docs/active.md"]["fencing_token"], 4)
        self.assertEqual(leases["docs/active.md"]["expected_revision"], 2)
        self.assertEqual(leases["docs/active.md"]["run_id"], str(self.active_run_id))
        self.assertEqual(leases["docs/stale.md"]["state"], "stale")
        self.assertEqual(leases["docs/fenced.md"]["state"], "fenced")
        self.assertEqual(leases["docs/fenced.md"]["fencing_status"], "superseded")
        self.assertNotIn("id", leases["docs/active.md"])
        self.assertNotIn("resource_id", leases["docs/active.md"])
        self.assertNotIn("task_lease_token", leases["docs/active.md"])

        active_only = client.get(
            f"/api/v1/projects/{self.project_id}/workspace/leases?state=active",
            headers=self._headers(self.member_id),
        )
        self.assertEqual(
            [item["resource_key"] for item in active_only.json()],
            ["docs/active.md"],
        )

    def test_project_member_lists_conflicts_and_promotion_deltas(self) -> None:
        conflicts = client.get(
            f"/api/v1/projects/{self.project_id}/workspace/conflicts",
            headers=self._headers(self.member_id),
        )
        self.assertEqual(conflicts.status_code, 200, conflicts.text)
        self.assertEqual(len(conflicts.json()), 1)
        conflict = conflicts.json()[0]
        self.assertEqual(conflict["run_id"], str(self.conflict_run_id))
        self.assertEqual(
            conflict["resources"],
            [
                {
                    "resource_key": "shared/output.md",
                    "expected_revision": 3,
                    "actual_revision": 4,
                }
            ],
        )
        self.assertNotIn("id", conflict)

        promotions = client.get(
            f"/api/v1/projects/{self.project_id}/workspace/promotions",
            headers=self._headers(self.member_id),
        )
        self.assertEqual(promotions.status_code, 200, promotions.text)
        promoted = next(
            item for item in promotions.json() if item["run_id"] == str(self.promoted_run_id)
        )
        self.assertEqual(promoted["status"], "promoted")
        self.assertEqual(
            promoted["resource_deltas"],
            [
                {
                    "resource_key": "docs/report.md",
                    "previous_revision": 4,
                    "resulting_revision": 5,
                    "revision_increment": 1,
                }
            ],
        )

    def test_project_and_installation_access_boundaries(self) -> None:
        for suffix in ("leases", "conflicts", "promotions"):
            denied = client.get(
                f"/api/v1/projects/{self.project_id}/workspace/{suffix}",
                headers=self._headers(self.outsider_id),
            )
            self.assertEqual(denied.status_code, 404, denied.text)
            admin_denied = client.get(
                f"/api/v1/admin/workspace/{suffix}",
                headers=self._headers(self.member_id),
            )
            self.assertEqual(admin_denied.status_code, 403, admin_denied.text)

        leases = client.get(
            "/api/v1/admin/workspace/leases",
            headers=self._headers(self.admin_id),
        )
        self.assertEqual(leases.status_code, 200, leases.text)
        self.assertTrue(
            {str(self.project_id), str(self.other_project_id)}
            <= {item["project_id"] for item in leases.json()}
        )
        conflicts = client.get(
            "/api/v1/admin/workspace/conflicts",
            headers=self._headers(self.admin_id),
        )
        self.assertEqual(conflicts.status_code, 200, conflicts.text)
        self.assertTrue(
            {str(self.project_id), str(self.other_project_id)}
            <= {item["project_id"] for item in conflicts.json()}
        )


if __name__ == "__main__":
    unittest.main()
