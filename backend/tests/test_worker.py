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
    Artifact,
    ArtifactVersion,
    AuditEvent,
    Budget,
    CostLedgerEntry,
    Goal,
    McpServer,
    McpServerVersion,
    Policy,
    Project,
    Run,
    Skill,
    SkillVersion,
    Task,
    Team,
    User,
)
from agentic_os.worker import claim_ready_task, run_task_worker_once
from agentic_os.worker.runner import TaskExecutionError

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
        raise unittest.SkipTest(
            f"PostgreSQL is not reachable at {TEST_DATABASE_URL!r}; set "
            "AGENTIC_OS_DATABASE_URL to run worker tests: "
            f"{error}"
        )
    _apply_migrations_from_zero(TEST_DATABASE_URL)


class WorkerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_database_engine(TEST_DATABASE_URL)
        self.Session = session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _build_ready_task(self, session, *, tools: tuple[str, ...] = ("echo",)) -> Task:
        team = Team(name=f"Team {uuid.uuid4()}")
        session.add(team)
        session.flush()

        user = User(email=f"operator-{uuid.uuid4()}@example.test", display_name="Operator")
        session.add(user)
        session.flush()

        project = Project(team_id=team.id, created_by=user.id, name="Worker Project")
        session.add(project)
        session.flush()

        goal = Goal(project_id=project.id, created_by=user.id, title="Worker Goal", status="active")
        session.add(goal)
        session.flush()

        skill = Skill(team_id=team.id, created_by=user.id, name="Worker Skill")
        session.add(skill)
        session.flush()
        skill_version = SkillVersion(skill_id=skill.id, version_number=1, content_ref="skills/worker/v1")
        session.add(skill_version)
        session.flush()

        mcp_server = McpServer(team_id=team.id, created_by=user.id, name="Worker MCP Server")
        session.add(mcp_server)
        session.flush()
        mcp_server_version = McpServerVersion(
            mcp_server_id=mcp_server.id,
            version_number=1,
            connection_config={"tools": [{"name": "echo", "description": "Echo input"}]},
        )
        session.add(mcp_server_version)
        session.flush()

        agent = Agent(team_id=team.id, created_by=user.id, name="Worker Agent")
        session.add(agent)
        session.flush()

        budget = Budget(agent_id=agent.id, currency="USD", amount_minor_units=10_00, enforcement_mode="hard_stop")
        session.add(budget)
        session.flush()

        agent_version = AgentVersion(
            agent_id=agent.id,
            version_number=1,
            capability_manifest={
                "skill_version_id": str(skill_version.id),
                "mcp_server_version_id": str(mcp_server_version.id),
                "enabled_tools": list(tools),
            },
            model_profile_id=None,
            default_budget_id=budget.id,
        )
        session.add(agent_version)
        session.flush()

        task = Task(
            goal_id=goal.id,
            title="Governed worker task",
            status="pending",
            assigned_agent_version_id=agent_version.id,
        )
        session.add(task)
        session.flush()
        session.commit()
        return task

    def test_worker_executes_single_task_end_to_end(self) -> None:
        with self.Session() as session:
            task = self._build_ready_task(session)
            task_id = task.id

        with self.Session() as session:
            claimed = run_task_worker_once(session, "worker-a")
            session.commit()
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.id, task_id)
            self.assertEqual(claimed.status, "completed")

        with self.Session() as session:
            task = session.get(Task, task_id)
            self.assertEqual(task.status, "completed")
            self.assertIsNone(task.lease_owner)
            self.assertIsNone(task.lease_expires_at)
            self.assertEqual(task.lease_token, 1)

            run = session.execute(select(Run).where(Run.task_id == task_id)).scalar_one()
            self.assertEqual(run.status, "completed")
            self.assertEqual(run.attempt_number, 1)
            self.assertEqual(run.idempotency_key, f"{task_id}:1")
            self.assertEqual(run.snapshot["policy_decision"], "allow")
            self.assertEqual(run.snapshot["enabled_tools"], ["echo"])
            self.assertIsNotNone(run.snapshot["skill_version_id"])
            self.assertIsNotNone(run.snapshot["mcp_server_version_id"])

            event_types = [
                row.event_type
                for row in session.execute(
                    select(AuditEvent).where(AuditEvent.run_id == run.id).order_by(AuditEvent.sequence_number)
                ).scalars()
            ]
            self.assertEqual(
                event_types,
                ["run.started", "policy.decision", "tool.invoked", "skill.invoked", "run.completed"],
            )

            ledger_entries = list(
                session.execute(select(CostLedgerEntry).where(CostLedgerEntry.run_id == run.id)).scalars()
            )
            self.assertEqual(len(ledger_entries), 1)
            self.assertTrue(ledger_entries[0].is_zero_cost)
            self.assertEqual(ledger_entries[0].action_type, "mcp_tool_call")
            self.assertEqual(ledger_entries[0].status, "reconciled")

            artifact_versions = list(
                session.execute(
                    select(ArtifactVersion)
                    .join(Artifact, ArtifactVersion.artifact_id == Artifact.id)
                    .where(Artifact.run_id == run.id)
                ).scalars()
            )
            self.assertEqual(len(artifact_versions), 1)

        # Re-running the worker must not duplicate the already-completed task.
        with self.Session() as session:
            second_claim = run_task_worker_once(session, "worker-b")
            session.commit()
            self.assertIsNone(second_claim)

        with self.Session() as session:
            runs = list(session.execute(select(Run).where(Run.task_id == task_id)).scalars())
            self.assertEqual(len(runs), 1)

    def test_lease_prevents_concurrent_claim_until_expiry(self) -> None:
        with self.Session() as session:
            task = self._build_ready_task(session)
            task_id = task.id

        with self.Session() as session:
            claimed_a = claim_ready_task(session, "worker-a", lease_seconds=60)
            session.commit()
            self.assertIsNotNone(claimed_a)
            self.assertEqual(claimed_a.lease_owner, "worker-a")
            self.assertEqual(claimed_a.lease_token, 1)

        with self.Session() as session:
            claimed_b = claim_ready_task(session, "worker-b", lease_seconds=60)
            session.commit()
            self.assertIsNone(claimed_b)

        # Simulate lease expiry (worker-a crashed without renewing/releasing).
        with self.Session() as session:
            task = session.get(Task, task_id)
            task.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.commit()

        with self.Session() as session:
            claimed_b = claim_ready_task(session, "worker-b", lease_seconds=60)
            session.commit()
            self.assertIsNotNone(claimed_b)
            self.assertEqual(claimed_b.lease_owner, "worker-b")
            self.assertEqual(claimed_b.lease_token, 2)

    def test_interrupted_run_is_reconciled_without_duplicating_completed_work(self) -> None:
        with self.Session() as session:
            task = self._build_ready_task(session)
            task_id = task.id

        # worker-a claims the task and gets partway through an attempt, then crashes:
        # its lease is never renewed or released.
        with self.Session() as session:
            claim_ready_task(session, "worker-crashed", lease_seconds=60)
            session.commit()

        with self.Session() as session:
            task = session.get(Task, task_id)
            task.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.commit()

        with self.Session() as session:
            claimed = run_task_worker_once(session, "worker-recovering")
            session.commit()
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.status, "completed")

        with self.Session() as session:
            runs = list(
                session.execute(select(Run).where(Run.task_id == task_id).order_by(Run.attempt_number)).scalars()
            )
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].status, "completed")
            self.assertEqual(runs[0].attempt_number, 1)

            task = session.get(Task, task_id)
            self.assertEqual(task.status, "completed")

    def test_policy_deny_blocks_execution_and_fails_task(self) -> None:
        with self.Session() as session:
            task = self._build_ready_task(session)
            task_id = task.id
            run = session.execute(select(Run).where(Run.task_id == task_id)).scalar_one_or_none()
            self.assertIsNone(run)
            agent_version = session.get(AgentVersion, task.assigned_agent_version_id)
            session.add(Policy(scope_type="agent", scope_id=agent_version.agent_id, decision="deny", rule={}))
            session.commit()

        with self.Session() as session:
            with self.assertRaises(TaskExecutionError):
                run_task_worker_once(session, "worker-a")
            session.commit()

        with self.Session() as session:
            task = session.get(Task, task_id)
            self.assertEqual(task.status, "failed")
            self.assertIsNone(task.lease_owner)

            run = session.execute(select(Run).where(Run.task_id == task_id)).scalar_one()
            self.assertEqual(run.status, "failed")

            event_types = {
                row.event_type
                for row in session.execute(select(AuditEvent).where(AuditEvent.task_id == task_id)).scalars()
            }
            self.assertIn("policy.decision", event_types)
            self.assertIn("task.failed", event_types)
            self.assertNotIn("tool.invoked", event_types)

    def test_claim_ignores_tasks_without_assigned_agent(self) -> None:
        with self.Session() as session:
            team = Team(name=f"Unassigned Team {uuid.uuid4()}")
            session.add(team)
            session.flush()
            user = User(email=f"unassigned-{uuid.uuid4()}@example.test", display_name="Operator")
            session.add(user)
            session.flush()
            project = Project(team_id=team.id, created_by=user.id, name="Unassigned Project")
            session.add(project)
            session.flush()
            goal = Goal(project_id=project.id, created_by=user.id, title="Unassigned Goal")
            session.add(goal)
            session.flush()
            session.add(Task(goal_id=goal.id, title="No agent assigned yet", status="pending"))
            session.commit()

        with self.Session() as session:
            claimed = claim_ready_task(session, "worker-a")
            session.commit()
            self.assertIsNone(claimed)


if __name__ == "__main__":
    unittest.main()
