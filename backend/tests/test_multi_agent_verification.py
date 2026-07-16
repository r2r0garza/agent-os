from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sqlalchemy import create_engine, select

from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import Run, Task, WorkspacePromotion, WorkspaceResource

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
    global TEST_DATABASE_URL, client
    TEST_DATABASE_URL = os.environ.get("AGENTIC_OS_DATABASE_URL", database_url())
    try:
        probe = create_database_engine(TEST_DATABASE_URL)
        with probe.connect():
            pass
        probe.dispose()
    except Exception as error:  # pragma: no cover - environment guard
        raise unittest.SkipTest(
            f"PostgreSQL is not reachable at {TEST_DATABASE_URL!r}; set "
            "AGENTIC_OS_DATABASE_URL to run multi-agent verification: "
            f"{error}"
        )
    os.environ["AGENTIC_OS_DATABASE_URL"] = TEST_DATABASE_URL
    _apply_migrations_from_zero(TEST_DATABASE_URL)

    from fastapi.testclient import TestClient

    from agentic_os.api import deps as api_deps
    from agentic_os.api.app import create_app

    # See test_restart_recovery.py: the API layer caches a process-wide
    # engine that must be cleared after this module resets the schema so
    # requests below do not reuse pooled connections referencing dropped
    # table OIDs.
    api_deps._engine.cache_clear()

    client = TestClient(create_app())


class MultiAgentVerificationTests(unittest.TestCase):
    """Sprint 2 exit criterion 6: end-to-end verification, through the
    versioned API and a real OS worker process, that dependent tasks wait,
    independent safe tasks run concurrently, conflicting tasks never
    overwrite each other unsafely, and a mid-run restart of a worker
    handling several simultaneous tasks recovers without losing or
    duplicating acknowledged work.
    """

    @classmethod
    def setUpClass(cls) -> None:
        # Agent selection (see domain/assignment.py) picks the first eligible
        # agent version for a required capability across the whole team, in
        # agent creation order -- there is no per-project scoping. Building
        # the two capability-distinct agents once for the class, and reusing
        # them across both test methods' goals, keeps assignment
        # deterministic instead of racing two independently created
        # "research"/"writing" agents against each other.
        cls.env = cls._build_project_and_agents("verify-multi-agent")

    def setUp(self) -> None:
        self.engine = create_database_engine(TEST_DATABASE_URL)
        self.Session = session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _run_worker_subprocess(
        self, *, worker_id: str, workers: int, lease_seconds: int, pause_seconds: float | None = None
    ) -> subprocess.Popen:
        env = dict(os.environ, AGENTIC_OS_DATABASE_URL=TEST_DATABASE_URL, PYTHONPATH=str(BACKEND_ROOT / "src"))
        if pause_seconds is not None:
            env["AGENTIC_OS_WORKER_TEST_PAUSE_AFTER_RUN_STARTED_SECONDS"] = str(pause_seconds)
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agentic_os",
                "worker",
                "run-once",
                "--worker-id",
                worker_id,
                "--workers",
                str(workers),
                "--lease-seconds",
                str(lease_seconds),
            ],
            cwd=BACKEND_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    @classmethod
    def _build_project_and_agents(cls, label: str) -> dict:
        """Configures a model profile, project, and two distinctly capable
        agents (each with its own skill, MCP tool, and budget) through the
        versioned API, mirroring how an operator would compose a team for a
        goal rather than reusing one agent for every task.
        """
        client.post(
            "/api/v1/model-profiles",
            json={
                "name": f"{label}-profile",
                "base_url": "https://api.example.com/v1",
                "model_identifier": "gpt-test",
                "api_key": "sk-test-only",
            },
        ).raise_for_status()

        project = client.post("/api/v1/projects", json={"name": f"{label} Project"}).json()

        agent_versions: dict[str, str] = {}
        for capability in ("research", "writing"):
            skill = client.post("/api/v1/skills", json={"name": f"{label}-{capability}-skill"}).json()
            skill_version = client.post(
                f"/api/v1/skills/{skill['id']}/versions",
                json={"content_ref": f"skills/{label}/{capability}/v1", "resource_metadata": {}},
            ).json()

            mcp_server = client.post(
                "/api/v1/mcp-servers", json={"name": f"{label}-{capability}-mcp"}
            ).json()
            mcp_version = client.post(
                f"/api/v1/mcp-servers/{mcp_server['id']}/versions",
                json={"connection_config": {"tools": [{"name": "echo", "description": "Echo input"}]}},
            ).json()

            agent = client.post("/api/v1/agents", json={"name": f"{label}-{capability}-agent"}).json()
            budget = client.post(
                f"/api/v1/agents/{agent['id']}/budgets",
                json={"currency": "USD", "amount_minor_units": 100_00, "enforcement_mode": "hard_stop"},
            ).json()

            agent_version = client.post(
                f"/api/v1/agents/{agent['id']}/versions",
                json={
                    "instructions": f"Use the echo tool for {capability} work.",
                    "capability_manifest": {
                        "capabilities": [capability],
                        "skill_version_id": skill_version["id"],
                        "mcp_server_version_id": mcp_version["id"],
                        "enabled_tools": ["echo"],
                    },
                    "default_budget_id": budget["id"],
                    "skill_attachments": [{"version_id": skill_version["id"], "config": {}}],
                    "mcp_server_attachments": [{"version_id": mcp_version["id"], "config": {}}],
                },
            ).json()
            agent_versions[capability] = agent_version["id"]

        return {"project_id": project["id"], "agent_versions": agent_versions}

    def _new_goal(self, title: str) -> str:
        goal = client.post(f"/api/v1/projects/{self.env['project_id']}/goals", json={"title": title}).json()
        return goal["id"]

    def _submit_task_graph(self, goal_id: str, *, key_prefix: str) -> dict[str, str]:
        """Persists, through the task-graph API, a goal decomposition that
        exercises every Sprint 2 exit-criterion-6 scenario in one graph:
        a dependent chain, a genuinely conflicting resource-key pair, and a
        disjoint pair safe to run in parallel. ``key_prefix`` keeps two
        scenario runs sharing one project from contending over the same
        workspace resource keys.
        """
        payload = {
            "tasks": [
                {
                    "client_id": "root",
                    "title": "Root task",
                    "required_capabilities": {"research": True},
                    "resource_intent": [{"resource_key": f"{key_prefix}/coordination.md", "intent": "write"}],
                },
                {
                    "client_id": "downstream",
                    "title": "Downstream task",
                    "required_capabilities": {"writing": True},
                    "resource_intent": [{"resource_key": f"{key_prefix}/downstream.md", "intent": "write"}],
                    "depends_on": ["root"],
                },
                {
                    "client_id": "conflict-a",
                    "title": "Conflict writer A",
                    "required_capabilities": {"research": True},
                    "resource_intent": [{"resource_key": f"{key_prefix}/conflict.md", "intent": "write"}],
                },
                {
                    "client_id": "conflict-b",
                    "title": "Conflict writer B",
                    "required_capabilities": {"writing": True},
                    "resource_intent": [{"resource_key": f"{key_prefix}/conflict.md", "intent": "write"}],
                },
                {
                    "client_id": "parallel-a",
                    "title": "Parallel writer A",
                    "required_capabilities": {"research": True},
                    "resource_intent": [{"resource_key": f"{key_prefix}/parallel-a.md", "intent": "write"}],
                },
                {
                    "client_id": "parallel-b",
                    "title": "Parallel writer B",
                    "required_capabilities": {"writing": True},
                    "resource_intent": [{"resource_key": f"{key_prefix}/parallel-b.md", "intent": "write"}],
                },
            ]
        }
        graph = client.post(f"/api/v1/goals/{goal_id}/task-graph", json=payload)
        graph.raise_for_status()
        return {node["title"]: node["id"] for node in graph.json()["tasks"]}

    def _assign_all(self, task_ids: dict[str, str], expected_capability: dict[str, str], expected_versions: dict) -> None:
        for title, task_id in task_ids.items():
            assignment = client.post(f"/api/v1/tasks/{task_id}/assignment")
            assignment.raise_for_status()
            body = assignment.json()
            self.assertEqual(body["status"], "assigned", f"{title} failed to assign: {body}")
            capability = expected_capability[title]
            self.assertEqual(body["selected_agent_version_id"], expected_versions[capability])

    def _run_interval(self, task_id: str) -> tuple[datetime, datetime]:
        runs = client.get(f"/api/v1/tasks/{task_id}/runs").json()
        completed = [run for run in runs if run["status"] == "completed"]
        self.assertEqual(len(completed), 1, f"expected exactly one completed run for {task_id}: {runs}")
        run = completed[0]
        return datetime.fromisoformat(run["started_at"]), datetime.fromisoformat(run["completed_at"])

    def test_multi_agent_task_graph_respects_dependencies_and_safe_parallelism(self) -> None:
        env = self.env
        goal_id = self._new_goal("verify-graph goal")
        task_id_by_client_id = self._submit_task_graph(goal_id, key_prefix="graph")
        expected_capability = {
            "Root task": "research",
            "Downstream task": "writing",
            "Conflict writer A": "research",
            "Conflict writer B": "writing",
            "Parallel writer A": "research",
            "Parallel writer B": "writing",
        }
        self._assign_all(task_id_by_client_id, expected_capability, env["agent_versions"])

        process = self._run_worker_subprocess(worker_id="verify-graph", workers=3, lease_seconds=30)
        stdout, stderr = process.communicate(timeout=60)
        self.assertEqual(process.returncode, 0, stderr)

        graph = client.get(f"/api/v1/goals/{goal_id}/task-graph").json()
        statuses = {task["title"]: task["status"] for task in graph["tasks"]}
        self.assertEqual(set(statuses.values()), {"completed"})

        # Multi-agent: the two capability tracks were actually assigned to
        # two different agent versions, not one agent doing everything.
        assigned_versions = {task["title"]: task["assigned_agent_version_id"] for task in graph["tasks"]}
        self.assertEqual(assigned_versions["Root task"], env["agent_versions"]["research"])
        self.assertEqual(assigned_versions["Downstream task"], env["agent_versions"]["writing"])
        self.assertNotEqual(env["agent_versions"]["research"], env["agent_versions"]["writing"])

        # Dependency ordering: downstream's run cannot have started before
        # root's run committed as completed.
        root_started, root_completed = self._run_interval(task_id_by_client_id["Root task"])
        downstream_started, _ = self._run_interval(task_id_by_client_id["Downstream task"])
        self.assertGreaterEqual(downstream_started, root_completed)

        # Conflict scenario: both writers targeted the same resource key, so
        # their run windows must never overlap even though separate agents
        # and separate worker threads were available to run them.
        a_started, a_completed = self._run_interval(task_id_by_client_id["Conflict writer A"])
        b_started, b_completed = self._run_interval(task_id_by_client_id["Conflict writer B"])
        self.assertTrue(a_completed <= b_started or b_completed <= a_started, "conflicting tasks overlapped in time")

        # Every declared write promoted cleanly; no explicit conflict state
        # was ever recorded, because the lease/lock protocol serialized the
        # genuinely conflicting pair before either could promote unsafely.
        with self.Session() as session:
            promotions = list(
                session.execute(
                    select(WorkspacePromotion).where(WorkspacePromotion.project_id == uuid.UUID(env["project_id"]))
                ).scalars()
            )
            graph_promotions = [p for p in promotions if p.task_id in {uuid.UUID(v) for v in task_id_by_client_id.values()}]
            self.assertTrue(graph_promotions)
            self.assertEqual({promotion.status for promotion in graph_promotions}, {"promoted"})

            resources = {
                row.resource_key: row.revision
                for row in session.execute(
                    select(WorkspaceResource).where(WorkspaceResource.project_id == uuid.UUID(env["project_id"]))
                ).scalars()
                if row.resource_key.startswith("graph/")
            }
            self.assertEqual(
                resources,
                {
                    "graph/coordination.md": 1,
                    "graph/downstream.md": 1,
                    "graph/conflict.md": 2,
                    "graph/parallel-a.md": 1,
                    "graph/parallel-b.md": 1,
                },
            )

        # Progress/audit evidence for the whole graph remains inspectable
        # through the API, not only via direct database access.
        audit_events = client.get("/api/v1/audit-events", params={"goal_id": goal_id, "limit": 500}).json()
        event_types = {event["event_type"] for event in audit_events}
        self.assertIn("workspace.promoted", event_types)
        self.assertIn("run.completed", event_types)

    def test_restart_recovers_multiple_simultaneous_in_flight_tasks(self) -> None:
        env = self.env
        goal_id = self._new_goal("verify-restart goal")
        task_ids = self._submit_task_graph(goal_id, key_prefix="restart")
        expected_capability = {
            "Root task": "research",
            "Downstream task": "writing",
            "Conflict writer A": "research",
            "Conflict writer B": "writing",
            "Parallel writer A": "research",
            "Parallel writer B": "writing",
        }
        self._assign_all(task_ids, expected_capability, env["agent_versions"])

        # Every task pauses right after its run is durably committed as
        # "running". With four workers and five initially-ready tasks
        # (root, one of the conflict pair, and both parallel writers;
        # downstream is blocked on root), several tasks reach "running"
        # simultaneously before any of them can finish.
        first_process = self._run_worker_subprocess(
            worker_id="verify-restart-1", workers=4, lease_seconds=3, pause_seconds=20
        )
        try:
            running_task_ids = self._wait_for_multiple_running(min_count=2, timeout=15.0)
            self.assertGreaterEqual(len(running_task_ids), 2)

            first_process.kill()
            first_process.wait(timeout=10)
        finally:
            if first_process.poll() is None:
                first_process.kill()
                first_process.wait(timeout=10)
            first_process.stdout.close()
            first_process.stderr.close()

        # The killed process's in-flight tasks remain durably "running" in
        # PostgreSQL -- an explicit, inspectable recoverable state -- even
        # though downstream never got a chance to start (root was not yet
        # complete when the process died).
        with self.Session() as session:
            downstream = session.get(Task, uuid.UUID(task_ids["Downstream task"]))
            self.assertIn(downstream.status, {"pending", "ready"})
            for task_id in running_task_ids:
                task = session.get(Task, uuid.UUID(task_id))
                self.assertEqual(task.status, "running")

        # Let the short leases expire, then bring a fresh worker back up
        # with no pause, draining the rest of the graph -- the equivalent
        # of an operator restarting the worker after a crash.
        time.sleep(4)
        second_process = self._run_worker_subprocess(
            worker_id="verify-restart-2", workers=4, lease_seconds=30
        )
        stdout, stderr = second_process.communicate(timeout=60)
        self.assertEqual(second_process.returncode, 0, stderr)

        graph = client.get(f"/api/v1/goals/{goal_id}/task-graph").json()
        statuses = {task["title"]: task["status"] for task in graph["tasks"]}
        self.assertEqual(set(statuses.values()), {"completed"})

        # Every task that was interrupted mid-flight recorded a
        # run.interrupted audit event for its stale attempt and completed on
        # a fresh attempt, instead of silently resuming or duplicating the
        # step; tasks that never started before the kill needed only one
        # attempt.
        for task_id in running_task_ids:
            runs = client.get(f"/api/v1/tasks/{task_id}/runs").json()
            self.assertEqual(len(runs), 2, runs)
            self.assertEqual(runs[0]["status"], "failed")
            self.assertEqual(runs[1]["status"], "completed")
            audit_events = client.get("/api/v1/audit-events", params={"task_id": task_id}).json()
            self.assertIn("run.interrupted", {event["event_type"] for event in audit_events})

        downstream_runs = client.get(f"/api/v1/tasks/{task_ids['Downstream task']}/runs").json()
        self.assertEqual(len(downstream_runs), 1)
        self.assertEqual(downstream_runs[0]["status"], "completed")

        # A further restart with nothing left to do claims zero tasks.
        third_process = self._run_worker_subprocess(worker_id="verify-restart-3", workers=2, lease_seconds=30)
        stdout3, stderr3 = third_process.communicate(timeout=30)
        self.assertEqual(third_process.returncode, 0, stderr3)
        self.assertIn("claimed and processed 0 task(s)", stdout3)

    def _wait_for_multiple_running(self, *, min_count: int, timeout: float) -> list[str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.Session() as session:
                running = list(
                    session.execute(select(Run.task_id).where(Run.status == "running")).scalars()
                )
            if len(running) >= min_count:
                return [str(task_id) for task_id in running]
            time.sleep(0.2)
        self.fail(f"fewer than {min_count} tasks reached 'running' within {timeout}s")


if __name__ == "__main__":
    unittest.main()
