from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from sqlalchemy import create_engine, select

from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import (
    Agent,
    AgentVersion,
    AgentVersionMcpServer,
    AgentVersionSkill,
    AuditEvent,
    Budget,
    GoalPlanExecution,
    McpServer,
    McpServerVersion,
    PlanTaskContextPackage,
    Policy,
    Run,
    Skill,
    SkillVersion,
    Task,
    TaskDependency,
)
from agentic_os.worker import run_scheduler_once, run_task_worker_once
from factories import make_goal, make_project, make_project_member, make_team, make_team_membership, make_user

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
            "AGENTIC_OS_DATABASE_URL to run plan-execution dispatch verification: "
            f"{error}"
        )
    os.environ["AGENTIC_OS_DATABASE_URL"] = TEST_DATABASE_URL
    _apply_migrations_from_zero(TEST_DATABASE_URL)

    from agentic_os.api.deps import _engine

    if _engine.cache_info().currsize:
        _engine().dispose()
        _engine.cache_clear()
    from fastapi.testclient import TestClient

    from agentic_os.api.app import create_app

    client = TestClient(create_app())


class PlanExecutionDispatchTests(unittest.TestCase):
    """Issue #93: an accepted capability-aware plan dispatches its tasks
    across multiple pinned agent versions through the real scheduler/worker,
    honoring dependencies and safe parallelism, and the durable
    ``GoalPlanExecution`` progress envelope stays live as dispatch happens
    rather than only updating when an operator polls the execution endpoint.
    """

    def setUp(self) -> None:
        self.engine = create_database_engine(TEST_DATABASE_URL)
        self.Session = session_factory(self.engine)
        with self.Session() as session:
            self.team = make_team(session, name=f"Dispatch team {uuid.uuid4()}")
            self.owner = make_user(session, display_name="Owner")
            make_team_membership(session, self.team, self.owner, role="owner")
            self.project = make_project(session, self.team, self.owner, name="Dispatch project")
            make_project_member(session, self.project, self.owner, granted_by=self.owner)
            self.goal = make_goal(session, self.project, self.owner, title="Ship the plan", status="active")

            self.agent_versions = {
                capability: self._build_agent_version(session, capability)
                for capability in ("research", "writing")
            }

            self.root = Task(
                goal_id=self.goal.id,
                created_by=self.owner.id,
                title="Root task",
                required_capabilities={"research": True},
                resource_intent=[{"resource_key": "dispatch/coordination.md", "intent": "write"}],
            )
            self.downstream = Task(
                goal_id=self.goal.id,
                created_by=self.owner.id,
                title="Downstream task",
                required_capabilities={"writing": True},
                resource_intent=[{"resource_key": "dispatch/downstream.md", "intent": "write"}],
            )
            self.conflict_a = Task(
                goal_id=self.goal.id,
                created_by=self.owner.id,
                title="Conflict writer A",
                required_capabilities={"research": True},
                resource_intent=[{"resource_key": "dispatch/conflict.md", "intent": "write"}],
            )
            self.conflict_b = Task(
                goal_id=self.goal.id,
                created_by=self.owner.id,
                title="Conflict writer B",
                required_capabilities={"writing": True},
                resource_intent=[{"resource_key": "dispatch/conflict.md", "intent": "write"}],
            )
            session.add_all([self.root, self.downstream, self.conflict_a, self.conflict_b])
            session.flush()
            session.add(TaskDependency(task_id=self.downstream.id, depends_on_task_id=self.root.id))
            session.flush()

            for value in (
                self.team,
                self.owner,
                self.project,
                self.goal,
                self.root,
                self.downstream,
                self.conflict_a,
                self.conflict_b,
            ):
                session.expunge(value)
            for version in self.agent_versions.values():
                session.expunge(version)
            session.commit()

    def tearDown(self) -> None:
        self.engine.dispose()

    def _build_agent_version(self, session, capability: str) -> AgentVersion:
        skill = Skill(team_id=self.team.id, created_by=self.owner.id, name=f"Dispatch {capability} skill")
        session.add(skill)
        session.flush()
        skill_version = SkillVersion(
            skill_id=skill.id, version_number=1, content_ref=f"skills/dispatch/{capability}/v1"
        )
        session.add(skill_version)
        session.flush()

        mcp_server = McpServer(team_id=self.team.id, created_by=self.owner.id, name=f"Dispatch {capability} mcp")
        session.add(mcp_server)
        session.flush()
        mcp_server_version = McpServerVersion(
            mcp_server_id=mcp_server.id,
            version_number=1,
            connection_config={"tools": [{"name": "echo", "description": "Echo input"}]},
        )
        session.add(mcp_server_version)
        session.flush()

        agent = Agent(team_id=self.team.id, created_by=self.owner.id, name=f"Dispatch {capability} agent")
        session.add(agent)
        session.flush()
        budget = Budget(
            agent_id=agent.id, currency="USD", amount_minor_units=100_00, enforcement_mode="hard_stop"
        )
        session.add(budget)
        session.flush()

        agent_version = AgentVersion(
            agent_id=agent.id,
            version_number=1,
            capability_manifest={
                "capabilities": [capability],
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
                    agent_version_id=agent_version.id, skill_version_id=skill_version.id, attachment_config={}
                ),
                AgentVersionMcpServer(
                    agent_version_id=agent_version.id,
                    mcp_server_version_id=mcp_server_version.id,
                    attachment_config={},
                ),
            ]
        )
        session.flush()
        return agent_version

    def _headers(self) -> dict[str, str]:
        return {"X-Agentic-User-ID": str(self.owner.id)}

    def _accept_plan(self) -> dict:
        preview = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions",
            json={},
            headers=self._headers(),
        )
        self.assertEqual(preview.status_code, 201, preview.text)
        preview_body = preview.json()
        assigned_candidate = {
            item["assignment_key"]: item["candidate_id"] for item in preview_body["assignments"]
        }
        candidate_versions = {item["id"]: item["agent_version_id"] for item in preview_body["candidates"]}
        # Confirm the plan actually spans two distinct pinned agent versions
        # before it is ever accepted -- this is the multi-agent shape #93
        # must be able to dispatch, not an artifact of how the test built it.
        resolved = {
            task_id: candidate_versions[candidate_id] for task_id, candidate_id in assigned_candidate.items()
        }
        self.assertEqual(resolved[str(self.root.id)], str(self.agent_versions["research"].id))
        self.assertEqual(resolved[str(self.downstream.id)], str(self.agent_versions["writing"].id))
        self.assertEqual(resolved[str(self.conflict_a.id)], str(self.agent_versions["research"].id))
        self.assertEqual(resolved[str(self.conflict_b.id)], str(self.agent_versions["writing"].id))

        accept = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions/{preview_body['id']}/accept",
            headers=self._headers(),
        )
        self.assertEqual(accept.status_code, 200, accept.text)
        accepted_body = accept.json()
        self.assertEqual(accepted_body["plan_execution"]["status"], "pending")
        self.assertEqual(len(accepted_body["materialized_tasks"]), 4)
        return accepted_body

    def _drain_scheduler(self, worker_id_prefix: str, *, worker_count: int, rounds: int = 5) -> list[str]:
        """Repeatedly runs the scheduler to quiescence, mirroring how the
        real worker loop (``deploy/worker-loop.sh``) polls continuously.

        A single ``run_scheduler_once`` pass can legitimately stop early
        while claimable work remains: a worker thread that hits a
        dispatch-time failure (e.g. a policy denial) exits that thread's
        claim loop entirely rather than retrying, so surviving capacity can
        transiently look idle before a resource lock another failed thread
        was holding is actually released. Production recovers this on the
        next poll rather than treating it as lost work, so tests drive the
        same repeated-poll behavior instead of assuming one pass always
        exhausts every claimable task.
        """
        errors: list[str] = []
        for _ in range(rounds):
            result = run_scheduler_once(self.Session, worker_id_prefix, worker_count=worker_count)
            errors.extend(result.errors)
            if not result.claimed and not result.errors:
                break
        return errors

    def _run_interval(self, task_id: uuid.UUID) -> tuple[datetime, datetime]:
        with self.Session() as session:
            run = session.execute(
                select(Run).where(Run.task_id == task_id, Run.status == "completed")
            ).scalar_one()
            return run.started_at, run.completed_at

    def test_accepted_plan_dispatches_across_pinned_agents_with_dependencies_and_progress(self) -> None:
        accepted_body = self._accept_plan()
        plan_execution_id = uuid.UUID(accepted_body["plan_execution"]["id"])

        errors = self._drain_scheduler("plan-dispatch", worker_count=3)
        self.assertEqual(errors, [])

        with self.Session() as session:
            tasks = {
                task.id: task
                for task in session.execute(
                    select(Task).where(
                        Task.id.in_(
                            [self.root.id, self.downstream.id, self.conflict_a.id, self.conflict_b.id]
                        )
                    )
                ).scalars()
            }
            self.assertEqual({task.status for task in tasks.values()}, {"completed"})

            # A single accepted plan produced task runs assigned to at least
            # two different pinned agent versions, not one agent doing
            # everything.
            self.assertEqual(tasks[self.root.id].assigned_agent_version_id, self.agent_versions["research"].id)
            self.assertEqual(
                tasks[self.downstream.id].assigned_agent_version_id, self.agent_versions["writing"].id
            )
            self.assertNotEqual(self.agent_versions["research"].id, self.agent_versions["writing"].id)

            # The plan-execution envelope reflects real dispatch outcomes: a
            # direct row read (not the recompute-on-read API endpoint) shows
            # the worker itself kept progress live during claim/completion.
            execution = session.get(GoalPlanExecution, plan_execution_id)
            self.assertEqual(execution.status, "completed")
            self.assertEqual(execution.total_tasks, 4)
            self.assertEqual(execution.completed_tasks, 4)
            self.assertEqual(execution.failed_tasks, 0)
            self.assertIsNotNone(execution.started_at)
            self.assertIsNotNone(execution.completed_at)

            packages = list(
                session.execute(
                    select(PlanTaskContextPackage).where(
                        PlanTaskContextPackage.plan_execution_id == plan_execution_id
                    )
                ).scalars()
            )
            self.assertEqual(len(packages), 4)
            for package in packages:
                run = session.get(Run, package.run_id)
                self.assertIsNotNone(run)
                self.assertEqual(run.task_id, package.task_id)
                self.assertEqual(run.status, "completed")

        # Dependent tasks wait for successful upstream completion.
        root_started, root_completed = self._run_interval(self.root.id)
        downstream_started, _ = self._run_interval(self.downstream.id)
        self.assertGreaterEqual(downstream_started, root_completed)

        # Two independent tasks racing for the same resource key never
        # overlap even though separate agents and worker threads were
        # available -- pinned-agent dispatch never weakens the existing
        # lease/lock protocol.
        a_started, a_completed = self._run_interval(self.conflict_a.id)
        b_started, b_completed = self._run_interval(self.conflict_b.id)
        self.assertTrue(a_completed <= b_started or b_completed <= a_started, "conflicting tasks overlapped")

    def test_retry_after_transient_failure_preserves_attempt_history_without_reresolving_assignment(
        self,
    ) -> None:
        accepted_body = self._accept_plan()
        plan_execution_id = uuid.UUID(accepted_body["plan_execution"]["id"])

        with self.Session() as session:
            for task_id in (self.conflict_a.id, self.conflict_b.id):
                task = session.get(Task, task_id)
                task.status = "cancelled"
            session.commit()

        with self.Session() as session:
            claimed = run_task_worker_once(session, "plan-dispatch-root")
            session.commit()
            self.assertEqual(claimed.id, self.root.id)
            self.assertEqual(claimed.status, "completed")

        # Simulate a worker crash mid-attempt: the downstream task's first
        # run is durably committed, then the external tool call fails.
        with self.Session() as session:
            with mock.patch(
                "agentic_os.worker.runner.invoke_tool", side_effect=RuntimeError("simulated crash")
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    run_task_worker_once(session, "plan-dispatch-downstream-1")
            session.commit()

        with self.Session() as session:
            downstream = session.get(Task, self.downstream.id)
            self.assertEqual(downstream.status, "failed")
            first_run = session.execute(
                select(Run).where(Run.task_id == self.downstream.id)
            ).scalar_one()
            self.assertEqual(first_run.status, "failed")
            first_snapshot_id = first_run.snapshot["configuration_snapshot_id"]
            first_agent_version_id = first_run.agent_version_id

            # An operator or recovery process brings the task back to
            # claimable state after the crash, the way restart reconciliation
            # would for an expired lease.
            downstream.status = "pending"
            session.commit()

        with self.Session() as session:
            claimed = run_task_worker_once(session, "plan-dispatch-downstream-2")
            session.commit()
            self.assertEqual(claimed.id, self.downstream.id)
            self.assertEqual(claimed.status, "completed")

        with self.Session() as session:
            runs = list(
                session.execute(
                    select(Run).where(Run.task_id == self.downstream.id).order_by(Run.attempt_number)
                ).scalars()
            )
            self.assertEqual(len(runs), 2)
            self.assertEqual([run.status for run in runs], ["failed", "completed"])
            # Retry preserves attempt history and reuses -- rather than
            # re-resolves -- the pinned assignment and configuration
            # snapshot established by the accepted plan.
            self.assertEqual(runs[0].snapshot["configuration_snapshot_id"], first_snapshot_id)
            self.assertEqual(runs[1].snapshot["configuration_snapshot_id"], first_snapshot_id)
            self.assertEqual(runs[1].agent_version_id, first_agent_version_id)
            self.assertEqual(runs[1].agent_version_id, self.agent_versions["writing"].id)

            downstream = session.get(Task, self.downstream.id)
            self.assertEqual(downstream.status, "completed")
            self.assertEqual(downstream.assigned_agent_version_id, self.agent_versions["writing"].id)

            execution = session.get(GoalPlanExecution, plan_execution_id)
            self.assertEqual(execution.completed_tasks, 2)
            self.assertEqual(execution.cancelled_tasks, 2)
            self.assertEqual(execution.status, "cancelled")

    def test_policy_denial_before_dispatch_fails_closed_without_partial_side_effects(self) -> None:
        accepted_body = self._accept_plan()
        plan_execution_id = uuid.UUID(accepted_body["plan_execution"]["id"])

        with self.Session() as session:
            session.add(
                Policy(
                    scope_type="agent",
                    scope_id=self.agent_versions["writing"].agent_id,
                    decision="deny",
                    rule={},
                )
            )
            session.commit()

        errors = self._drain_scheduler("plan-dispatch-denied", worker_count=3)
        # The two research-capability tasks have no unmet dependency and no
        # denied policy, so they still complete; only the writing-capability
        # tasks fail closed on the policy check that runs immediately before
        # any tool side effect.
        self.assertEqual(len(errors), 2)

        with self.Session() as session:
            root = session.get(Task, self.root.id)
            conflict_a = session.get(Task, self.conflict_a.id)
            downstream = session.get(Task, self.downstream.id)
            conflict_b = session.get(Task, self.conflict_b.id)
            self.assertEqual(root.status, "completed")
            self.assertEqual(conflict_a.status, "completed")
            self.assertEqual(downstream.status, "failed")
            self.assertEqual(conflict_b.status, "failed")

            for task_id in (self.downstream.id, self.conflict_b.id):
                runs = list(session.execute(select(Run).where(Run.task_id == task_id)).scalars())
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0].status, "failed")
                event_types = {
                    row.event_type
                    for row in session.execute(
                        select(AuditEvent).where(AuditEvent.task_id == task_id)
                    ).scalars()
                }
                self.assertIn("policy.decision", event_types)
                self.assertIn("task.failed", event_types)
                # A denied policy fails before any tool side effect -- no
                # partial artifact/workspace mutation was ever attempted.
                self.assertNotIn("tool.invoked", event_types)

            execution = session.get(GoalPlanExecution, plan_execution_id)
            self.assertEqual(execution.status, "failed")
            self.assertEqual(execution.failed_tasks, 2)
            self.assertEqual(execution.completed_tasks, 2)


if __name__ == "__main__":
    unittest.main()
