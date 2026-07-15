from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sqlalchemy import create_engine

from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import (
    Agent,
    AgentVersion,
    Budget,
    Goal,
    McpServer,
    McpServerVersion,
    Project,
    Skill,
    SkillVersion,
    Task,
    TaskDependency,
    Team,
    User,
)
from agentic_os.worker import claim_ready_task, run_scheduler_once
from agentic_os.worker.leases import release_lease

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
            "AGENTIC_OS_DATABASE_URL to run scheduler tests: "
            f"{error}"
        )
    _apply_migrations_from_zero(TEST_DATABASE_URL)


class SchedulerTestCase(unittest.TestCase):
    """Exit criterion 3: the scheduler claims and runs ready tasks in
    dependency order, allowing safe parallel execution for independent
    tasks while respecting resource intent.
    """

    def setUp(self) -> None:
        self.engine = create_database_engine(TEST_DATABASE_URL)
        self.Session = session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _build_environment(self, session) -> tuple[Goal, AgentVersion]:
        team = Team(name=f"Team {uuid.uuid4()}")
        session.add(team)
        session.flush()

        user = User(email=f"operator-{uuid.uuid4()}@example.test", display_name="Operator")
        session.add(user)
        session.flush()

        project = Project(team_id=team.id, created_by=user.id, name="Scheduler Project")
        session.add(project)
        session.flush()

        goal = Goal(project_id=project.id, created_by=user.id, title="Scheduler Goal", status="active")
        session.add(goal)
        session.flush()

        skill = Skill(team_id=team.id, created_by=user.id, name="Scheduler Skill")
        session.add(skill)
        session.flush()
        skill_version = SkillVersion(skill_id=skill.id, version_number=1, content_ref="skills/scheduler/v1")
        session.add(skill_version)
        session.flush()

        mcp_server = McpServer(team_id=team.id, created_by=user.id, name="Scheduler MCP Server")
        session.add(mcp_server)
        session.flush()
        mcp_server_version = McpServerVersion(
            mcp_server_id=mcp_server.id,
            version_number=1,
            connection_config={"tools": [{"name": "echo", "description": "Echo input"}]},
        )
        session.add(mcp_server_version)
        session.flush()

        agent = Agent(team_id=team.id, created_by=user.id, name="Scheduler Agent")
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
                "enabled_tools": ["echo"],
            },
            model_profile_id=None,
            default_budget_id=budget.id,
        )
        session.add(agent_version)
        session.flush()
        session.commit()
        return goal, agent_version

    def _make_task(
        self,
        session,
        goal: Goal,
        agent_version: AgentVersion,
        *,
        title: str,
        resource_intent: list[dict] | None = None,
    ) -> Task:
        task = Task(
            goal_id=goal.id,
            title=title,
            status="pending",
            resource_intent=resource_intent or [],
            assigned_agent_version_id=agent_version.id,
            assignment_status="assigned",
        )
        session.add(task)
        session.flush()
        return task

    def test_dependency_ordering_blocks_downstream_claim_until_upstream_completes(self) -> None:
        with self.Session() as session:
            goal, agent_version = self._build_environment(session)
            upstream = self._make_task(session, goal, agent_version, title="Upstream")
            downstream = self._make_task(session, goal, agent_version, title="Downstream")
            session.add(TaskDependency(task_id=downstream.id, depends_on_task_id=upstream.id))
            session.commit()
            upstream_id, downstream_id = upstream.id, downstream.id

        with self.Session() as session:
            claimed = claim_ready_task(session, "worker-a")
            session.commit()
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.id, upstream_id)

        # The only other ready task is blocked by its unmet dependency.
        with self.Session() as session:
            claimed = claim_ready_task(session, "worker-b")
            session.commit()
            self.assertIsNone(claimed)

        with self.Session() as session:
            upstream = session.get(Task, upstream_id)
            release_lease(session, upstream, "worker-a", status="completed")
            session.commit()

        with self.Session() as session:
            claimed = claim_ready_task(session, "worker-b")
            session.commit()
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.id, downstream_id)

    def test_conflicting_resource_intent_blocks_claim_until_release(self) -> None:
        with self.Session() as session:
            goal, agent_version = self._build_environment(session)
            task_a = self._make_task(
                session,
                goal,
                agent_version,
                title="Writer A",
                resource_intent=[{"resource_key": "docs/report.md", "intent": "write"}],
            )
            task_b = self._make_task(
                session,
                goal,
                agent_version,
                title="Writer B",
                resource_intent=[{"resource_key": "docs/report.md", "intent": "write"}],
            )
            session.commit()
            task_a_id, task_b_id = task_a.id, task_b.id

        # The resource-key lock a claim takes is scoped to its transaction,
        # mirroring how a real run keeps its session/transaction open for the
        # duration of execution. Keep worker-a's session open (uncommitted)
        # while worker-b attempts to claim the conflicting task, instead of
        # simulating "running" purely through a committed status column.
        with self.Session() as session_a:
            claimed_a = claim_ready_task(session_a, "worker-a")
            self.assertIsNotNone(claimed_a)
            self.assertEqual(claimed_a.id, task_a_id)

            # task_b writes the same resource key task_a is currently holding.
            with self.Session() as session_b:
                claimed = claim_ready_task(session_b, "worker-b")
                session_b.commit()
                self.assertIsNone(claimed)

            release_lease(session_a, claimed_a, "worker-a", status="completed")
            session_a.commit()

        with self.Session() as session:
            claimed_b = claim_ready_task(session, "worker-b")
            session.commit()
            self.assertIsNotNone(claimed_b)
            self.assertEqual(claimed_b.id, task_b_id)

    def test_disjoint_resource_intent_does_not_block_claim(self) -> None:
        with self.Session() as session:
            goal, agent_version = self._build_environment(session)
            task_a = self._make_task(
                session,
                goal,
                agent_version,
                title="Writer A",
                resource_intent=[{"resource_key": "docs/a.md", "intent": "write"}],
            )
            task_b = self._make_task(
                session,
                goal,
                agent_version,
                title="Writer B",
                resource_intent=[{"resource_key": "docs/b.md", "intent": "write"}],
            )
            session.commit()
            task_b_id = task_b.id

        with self.Session() as session:
            claim_ready_task(session, "worker-a")
            session.commit()

        with self.Session() as session:
            claimed_b = claim_ready_task(session, "worker-b")
            session.commit()
            self.assertIsNotNone(claimed_b)
            self.assertEqual(claimed_b.id, task_b_id)

    def test_scheduler_runs_independent_ready_tasks_concurrently(self) -> None:
        with self.Session() as session:
            goal, agent_version = self._build_environment(session)
            self._make_task(session, goal, agent_version, title="Independent A")
            self._make_task(session, goal, agent_version, title="Independent B")
            session.commit()

        concurrency = {"current": 0, "max": 0}
        lock = threading.Lock()

        def fake_invoke_tool(name, arguments):
            with lock:
                concurrency["current"] += 1
                concurrency["max"] = max(concurrency["max"], concurrency["current"])
            time.sleep(0.3)
            with lock:
                concurrency["current"] -= 1
            return {"echo": arguments}

        with patch("agentic_os.worker.runner.invoke_tool", side_effect=fake_invoke_tool):
            result = run_scheduler_once(self.Session, "sched-parallel", worker_count=2)

        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.claimed), 2)
        # Two independent ready tasks with no declared resource conflict must
        # have actually overlapped in time, not merely both completed.
        self.assertEqual(concurrency["max"], 2)

        with self.Session() as session:
            for task_id, status in result.claimed:
                self.assertEqual(status, "completed")
                task = session.get(Task, uuid.UUID(task_id))
                self.assertEqual(task.status, "completed")

    def test_scheduler_serializes_tasks_with_conflicting_resource_intent(self) -> None:
        with self.Session() as session:
            goal, agent_version = self._build_environment(session)
            self._make_task(
                session,
                goal,
                agent_version,
                title="Conflict A",
                resource_intent=[{"resource_key": "shared/output.md", "intent": "write"}],
            )
            self._make_task(
                session,
                goal,
                agent_version,
                title="Conflict B",
                resource_intent=[{"resource_key": "shared/output.md", "intent": "write"}],
            )
            session.commit()

        concurrency = {"current": 0, "max": 0}
        lock = threading.Lock()

        def fake_invoke_tool(name, arguments):
            with lock:
                concurrency["current"] += 1
                concurrency["max"] = max(concurrency["max"], concurrency["current"])
            time.sleep(0.3)
            with lock:
                concurrency["current"] -= 1
            return {"echo": arguments}

        with patch("agentic_os.worker.runner.invoke_tool", side_effect=fake_invoke_tool):
            result = run_scheduler_once(self.Session, "sched-conflict", worker_count=2)

        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.claimed), 2)
        # Both tasks write the same resource key, so they must never overlap
        # even though two worker slots were available to run them.
        self.assertEqual(concurrency["max"], 1)

        with self.Session() as session:
            for task_id, status in result.claimed:
                self.assertEqual(status, "completed")

    def test_scheduler_preserves_single_worker_identity_for_lease_owner(self) -> None:
        with self.Session() as session:
            goal, agent_version = self._build_environment(session)
            task = self._make_task(session, goal, agent_version, title="Solo")
            session.commit()
            task_id = task.id

        result = run_scheduler_once(self.Session, "solo-worker", worker_count=1)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.claimed, [(str(task_id), "completed")])

        with self.Session() as session:
            task = session.get(Task, task_id)
            self.assertEqual(task.status, "completed")
            self.assertIsNone(task.lease_owner)


if __name__ == "__main__":
    unittest.main()
