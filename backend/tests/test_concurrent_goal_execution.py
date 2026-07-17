from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sqlalchemy import create_engine, select

from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import (
    Agent,
    AgentVersion,
    AgentVersionMcpServer,
    AgentVersionSkill,
    Budget,
    Goal,
    McpServer,
    McpServerVersion,
    Project,
    Run,
    Skill,
    SkillVersion,
    Task,
    Team,
    User,
    WorkspacePromotion,
    WorkspaceResource,
    WorkspaceResourceLease,
)
from agentic_os.worker import run_scheduler_once

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
            "AGENTIC_OS_DATABASE_URL to run concurrent-goal verification: "
            f"{error}"
        )
    os.environ["AGENTIC_OS_DATABASE_URL"] = TEST_DATABASE_URL
    _apply_migrations_from_zero(TEST_DATABASE_URL)

    from fastapi.testclient import TestClient

    from agentic_os.api import deps as api_deps
    from agentic_os.api.app import create_app

    api_deps._engine.cache_clear()
    global client
    client = TestClient(create_app())


class ConcurrentGoalExecutionTests(unittest.TestCase):
    """Issue #67: goal submission, decomposition, scheduling, and workspace
    promotion must stay safe when two or more goals in the same project
    execute concurrently, and a worker killed mid-promotion must recover
    cleanly instead of corrupting workspace state.
    """

    def setUp(self) -> None:
        self.engine = create_database_engine(TEST_DATABASE_URL)
        self.Session = session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _build_project(self, session) -> Project:
        team = Team(name=f"Concurrent Goals Team {uuid.uuid4()}")
        user = User(email=f"concurrent-{uuid.uuid4()}@example.test", display_name="Operator")
        session.add_all([team, user])
        session.flush()
        project = Project(team_id=team.id, created_by=user.id, name="Concurrent Goals Project")
        session.add(project)
        session.flush()

        skill = Skill(team_id=team.id, created_by=user.id, name=f"Concurrent Skill {uuid.uuid4()}")
        session.add(skill)
        session.flush()
        skill_version = SkillVersion(skill_id=skill.id, version_number=1, content_ref="skills/concurrent/v1")
        session.add(skill_version)
        session.flush()

        mcp_server = McpServer(team_id=team.id, created_by=user.id, name=f"Concurrent MCP Server {uuid.uuid4()}")
        session.add(mcp_server)
        session.flush()
        mcp_server_version = McpServerVersion(
            mcp_server_id=mcp_server.id,
            version_number=1,
            connection_config={"tools": [{"name": "echo", "description": "Echo input"}]},
        )
        session.add(mcp_server_version)
        session.flush()

        agent = Agent(team_id=team.id, created_by=user.id, name=f"Concurrent Agent {uuid.uuid4()}")
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
        session.add_all(
            [
                AgentVersionSkill(
                    agent_version_id=agent_version.id,
                    skill_version_id=skill_version.id,
                    attachment_config={},
                ),
                AgentVersionMcpServer(
                    agent_version_id=agent_version.id,
                    mcp_server_version_id=mcp_server_version.id,
                    attachment_config={},
                ),
            ]
        )
        session.flush()
        self._agent_version_id = agent_version.id
        session.commit()
        return project

    def _make_goal(self, session, project: Project, title: str) -> Goal:
        user = session.execute(select(User).limit(1)).scalars().first()
        goal = Goal(project_id=project.id, created_by=user.id, title=title, status="active")
        session.add(goal)
        session.flush()
        return goal

    def _make_task(self, session, goal: Goal, *, title: str, resource_intent: list[dict] | None = None) -> Task:
        task = Task(
            goal_id=goal.id,
            title=title,
            status="pending",
            resource_intent=resource_intent or [],
            assigned_agent_version_id=self._agent_version_id,
            assignment_status="assigned",
        )
        session.add(task)
        session.flush()
        return task

    def test_two_goals_with_disjoint_resource_keys_complete_concurrently(self) -> None:
        """Acceptance criterion 1: two goals submitted concurrently to the
        same project both reach "completed" when their resource keys are
        disjoint, and they actually overlap in wall-clock execution rather
        than merely both finishing eventually.
        """
        with self.Session() as session:
            project = self._build_project(session)
            goal_a = self._make_goal(session, project, "Goal A")
            goal_b = self._make_goal(session, project, "Goal B")
            self._make_task(
                session,
                goal_a,
                title="Goal A Task",
                resource_intent=[{"resource_key": "goal-a/output.md", "intent": "write"}],
            )
            self._make_task(
                session,
                goal_b,
                title="Goal B Task",
                resource_intent=[{"resource_key": "goal-b/output.md", "intent": "write"}],
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

        from unittest.mock import patch

        with patch("agentic_os.worker.runner.invoke_tool", side_effect=fake_invoke_tool):
            result = run_scheduler_once(self.Session, "sched-multi-goal-disjoint", worker_count=2)

        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.claimed), 2)
        self.assertEqual(concurrency["max"], 2)

        with self.Session() as session:
            for task_id, status in result.claimed:
                self.assertEqual(status, "completed")
            resources = list(
                session.execute(
                    select(WorkspaceResource).where(WorkspaceResource.project_id == project.id)
                ).scalars()
            )
            self.assertEqual(
                {resource.resource_key: resource.revision for resource in resources},
                {"goal-a/output.md": 1, "goal-b/output.md": 1},
            )
            promotions = list(
                session.execute(
                    select(WorkspacePromotion).where(WorkspacePromotion.project_id == project.id)
                ).scalars()
            )
            self.assertEqual({promotion.status for promotion in promotions}, {"promoted"})

    def test_two_goals_with_overlapping_resource_keys_serialize_without_corruption(self) -> None:
        """Acceptance criterion 2: two goals with overlapping resource keys
        must never corrupt workspace state. The workspace protocol resolves
        the overlap by serializing the conflicting tasks (only one may hold
        the resource lease at a time), so both goals still complete and the
        shared resource ends up at the correct revision reflecting both
        writes -- never lost, never double-applied, never run concurrently.
        """
        with self.Session() as session:
            project = self._build_project(session)
            goal_a = self._make_goal(session, project, "Goal A")
            goal_b = self._make_goal(session, project, "Goal B")
            self._make_task(
                session,
                goal_a,
                title="Goal A Shared Write",
                resource_intent=[{"resource_key": "shared/output.md", "intent": "write"}],
            )
            self._make_task(
                session,
                goal_b,
                title="Goal B Shared Write",
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

        from unittest.mock import patch

        with patch("agentic_os.worker.runner.invoke_tool", side_effect=fake_invoke_tool):
            result = run_scheduler_once(self.Session, "sched-multi-goal-overlap", worker_count=2)

        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.claimed), 2)
        # Both goals write the same resource key: the workspace protocol must
        # detect the overlap and serialize them rather than letting two
        # promotions race against the same revision.
        self.assertEqual(concurrency["max"], 1)

        with self.Session() as session:
            for task_id, status in result.claimed:
                self.assertEqual(status, "completed")
            resource = session.execute(
                select(WorkspaceResource).where(
                    WorkspaceResource.project_id == project.id,
                    WorkspaceResource.resource_key == "shared/output.md",
                )
            ).scalar_one()
            # Two sequential promotions against the same resource: the
            # revision must reflect both, not be stuck at 1 (a lost update)
            # or corrupted by a torn write.
            self.assertEqual(resource.revision, 2)
            promotions = list(
                session.execute(
                    select(WorkspacePromotion)
                    .where(WorkspacePromotion.project_id == project.id)
                    .order_by(WorkspacePromotion.created_at)
                ).scalars()
            )
            self.assertEqual(len(promotions), 2)
            self.assertEqual([promotion.status for promotion in promotions], ["promoted", "promoted"])
            resulting = sorted(
                promotion.resulting_revisions["shared/output.md"] for promotion in promotions
            )
            self.assertEqual(resulting, [1, 2])

    def test_concurrent_goal_decomposition_produces_independent_task_dags(self) -> None:
        """Two goals submitted to the same project and decomposed
        concurrently through the task-graph API must each end up with their
        own independent, complete task DAG -- no lost tasks, no cross-goal
        dependency leakage, no deadlock between the two decompose requests.
        """
        project = client.post("/api/v1/projects", json={"name": f"Decompose Project {uuid.uuid4()}"}).json()
        goal_a = client.post(
            f"/api/v1/projects/{project['id']}/goals", json={"title": "Concurrent Decompose A"}
        ).json()
        goal_b = client.post(
            f"/api/v1/projects/{project['id']}/goals", json={"title": "Concurrent Decompose B"}
        ).json()

        def decompose(goal_id: str) -> dict:
            response = client.post(f"/api/v1/goals/{goal_id}/task-graph/decompose", json={})
            response.raise_for_status()
            return response.json()

        with ThreadPoolExecutor(max_workers=2) as executor:
            future_a = executor.submit(decompose, goal_a["id"])
            future_b = executor.submit(decompose, goal_b["id"])
            graph_a = future_a.result(timeout=30)
            graph_b = future_b.result(timeout=30)

        for graph in (graph_a, graph_b):
            self.assertEqual(len(graph["tasks"]), 3)
            self.assertEqual(len(graph["dependencies"]), 2)

        task_ids_a = {task["id"] for task in graph_a["tasks"]}
        task_ids_b = {task["id"] for task in graph_b["tasks"]}
        self.assertEqual(task_ids_a & task_ids_b, set())

        dependency_task_ids_a = {edge["task_id"] for edge in graph_a["dependencies"]} | {
            edge["depends_on_task_id"] for edge in graph_a["dependencies"]
        }
        dependency_task_ids_b = {edge["task_id"] for edge in graph_b["dependencies"]} | {
            edge["depends_on_task_id"] for edge in graph_b["dependencies"]
        }
        self.assertTrue(dependency_task_ids_a.issubset(task_ids_a))
        self.assertTrue(dependency_task_ids_b.issubset(task_ids_b))

        tasks_a = client.get(f"/api/v1/goals/{goal_a['id']}/tasks").json()
        tasks_b = client.get(f"/api/v1/goals/{goal_b['id']}/tasks").json()
        self.assertEqual({task["id"] for task in tasks_a}, task_ids_a)
        self.assertEqual({task["id"] for task in tasks_b}, task_ids_b)

    def _run_worker_subprocess(
        self, *, worker_id: str, lease_seconds: int, promotion_pause_seconds: float | None = None
    ) -> subprocess.Popen:
        env = dict(os.environ, AGENTIC_OS_DATABASE_URL=TEST_DATABASE_URL, PYTHONPATH=str(BACKEND_ROOT / "src"))
        if promotion_pause_seconds is not None:
            # Force an immediate (zero-delay) commit at the run-started
            # checkpoint so the claim and resource-lease acquisition become
            # durably visible to other sessions before we pause at the later
            # post-promotion checkpoint. Without this, nothing in the
            # transaction commits until the run finishes, and there would be
            # nothing to observe from another session during the pause.
            env["AGENTIC_OS_WORKER_TEST_PAUSE_AFTER_RUN_STARTED_SECONDS"] = "0"
            env["AGENTIC_OS_WORKER_TEST_PAUSE_AFTER_PROMOTION_SECONDS"] = str(promotion_pause_seconds)
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agentic_os",
                "worker",
                "run-once",
                "--worker-id",
                worker_id,
                "--lease-seconds",
                str(lease_seconds),
            ],
            cwd=BACKEND_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_worker_killed_mid_promotion_recovers_via_stale_lease(self) -> None:
        """Acceptance criterion 3: a worker process killed mid-promotion must
        not leave the workspace in an inconsistent state, and recovery must
        pick up the stale lease and fencing token correctly. The kill lands
        right after ``promote_workspace_changes`` has staged its revision and
        lease mutations in an open (uncommitted) transaction, so the
        surrounding transaction's rollback must discard them entirely.
        """
        with self.Session() as session:
            project = self._build_project(session)
            goal = self._make_goal(session, project, "Promotion Kill Goal")
            task = self._make_task(
                session,
                goal,
                title="Promotion Kill Task",
                resource_intent=[{"resource_key": "kill/mid-promotion.md", "intent": "write"}],
            )
            session.commit()
            task_id = task.id

        first_process = self._run_worker_subprocess(
            worker_id="promotion-kill-1", lease_seconds=3, promotion_pause_seconds=20
        )
        try:
            deadline = time.monotonic() + 15.0
            promoted = False
            while time.monotonic() < deadline:
                line = first_process.stderr.readline()
                if "workspace promoted; pausing" in line:
                    promoted = True
                    break
            self.assertTrue(promoted, "worker did not reach the post-promotion pause in time")

            # While the first worker is paused with its promotion staged but
            # uncommitted, another session must see the resource untouched:
            # no dirty read of a not-yet-committed revision bump.
            with self.Session() as session:
                resource = session.execute(
                    select(WorkspaceResource).where(
                        WorkspaceResource.project_id == project.id,
                        WorkspaceResource.resource_key == "kill/mid-promotion.md",
                    )
                ).scalar_one_or_none()
                self.assertIsNotNone(resource)
                self.assertEqual(resource.revision, 0)
                task_row = session.get(Task, task_id)
                self.assertEqual(task_row.status, "running")
                self.assertEqual(task_row.lease_owner, "promotion-kill-1")

            first_process.kill()
            first_process.wait(timeout=10)
        finally:
            if first_process.poll() is None:
                first_process.kill()
                first_process.wait(timeout=10)
            first_process.stdout.close()
            first_process.stderr.close()

        # The killed transaction never committed: the resource must still be
        # untouched, not left half-applied.
        with self.Session() as session:
            resource = session.execute(
                select(WorkspaceResource).where(
                    WorkspaceResource.project_id == project.id,
                    WorkspaceResource.resource_key == "kill/mid-promotion.md",
                )
            ).scalar_one()
            self.assertEqual(resource.revision, 0)
            task_row = session.get(Task, task_id)
            self.assertEqual(task_row.status, "running")

        # Let the short lease expire, then restart with a fresh worker.
        time.sleep(4)
        second_process = self._run_worker_subprocess(worker_id="promotion-kill-2", lease_seconds=30)
        stdout, stderr = second_process.communicate(timeout=30)
        self.assertEqual(second_process.returncode, 0, stderr)

        with self.Session() as session:
            task_row = session.get(Task, task_id)
            self.assertEqual(task_row.status, "completed")
            self.assertIsNone(task_row.lease_owner)

            resource = session.execute(
                select(WorkspaceResource).where(
                    WorkspaceResource.project_id == project.id,
                    WorkspaceResource.resource_key == "kill/mid-promotion.md",
                )
            ).scalar_one()
            # Exactly one real promotion applied -- not zero (stuck) and not
            # two (a duplicate from the aborted attempt somehow persisting).
            self.assertEqual(resource.revision, 1)

            lease = session.execute(
                select(WorkspaceResourceLease).where(WorkspaceResourceLease.resource_id == resource.id)
            ).scalar_one()
            self.assertIsNone(lease.lease_owner)

            promotions = list(
                session.execute(
                    select(WorkspacePromotion).where(WorkspacePromotion.project_id == project.id)
                ).scalars()
            )
            self.assertEqual(len(promotions), 1)
            self.assertEqual(promotions[0].status, "promoted")
            self.assertEqual(promotions[0].resulting_revisions, {"kill/mid-promotion.md": 1})

            runs = list(session.execute(select(Run).where(Run.task_id == task_id)).scalars())
            # The killed attempt never durably committed a run row of its
            # own beyond the initial "running" state recorded before the
            # pause; the second attempt is the only run that reaches
            # "completed", proving no duplicate promotion or duplicate run.
            completed_runs = [run for run in runs if run.status == "completed"]
            self.assertEqual(len(completed_runs), 1)


if __name__ == "__main__":
    unittest.main()
