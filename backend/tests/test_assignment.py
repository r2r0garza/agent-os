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
from agentic_os.domain.assignment import assign_task, match_capabilities
from agentic_os.domain.models import (
    Agent,
    AgentVersion,
    Budget,
    CostLedgerEntry,
    Goal,
    Policy,
    Project,
    Task,
    Team,
    User,
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
        raise unittest.SkipTest(f"PostgreSQL is not reachable at {TEST_DATABASE_URL!r}: {error}")

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


class AssignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_database_engine(TEST_DATABASE_URL)
        self.Session = session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _task(self, session, required: dict | None = None) -> tuple[Task, User, Team]:
        team = Team(name=f"Assignment Team {uuid.uuid4()}")
        user = User(email=f"assignment-{uuid.uuid4()}@example.test", display_name="Operator")
        session.add_all([team, user])
        session.flush()
        project = Project(team_id=team.id, created_by=user.id, name="Assignment Project")
        session.add(project)
        session.flush()
        goal = Goal(project_id=project.id, created_by=user.id, title="Assignment Goal")
        session.add(goal)
        session.flush()
        task = Task(goal_id=goal.id, title="Assigned task", required_capabilities=required or {})
        session.add(task)
        session.flush()
        return task, user, team

    def _agent_version(
        self,
        session,
        user: User,
        team: Team,
        capabilities: list[str],
        *,
        default_budget_id: uuid.UUID | None = None,
        version_number: int = 1,
        agent: Agent | None = None,
    ) -> tuple[Agent, AgentVersion]:
        if agent is None:
            agent = Agent(team_id=team.id, created_by=user.id, name=f"Agent {uuid.uuid4()}")
            session.add(agent)
            session.flush()
        version = AgentVersion(
            agent_id=agent.id,
            version_number=version_number,
            capability_manifest={"capabilities": capabilities},
            default_budget_id=default_budget_id,
        )
        session.add(version)
        session.flush()
        return agent, version

    def test_explicit_capability_match_reports_missing_names(self) -> None:
        matched, missing = match_capabilities(
            {"research": True, "writing": True, "review": False},
            {"capabilities": ["research"]},
        )
        self.assertEqual(matched, ["research"])
        self.assertEqual(missing, ["writing"])

    def test_assignment_selects_latest_eligible_version_and_persists_evidence(self) -> None:
        with self.Session() as session:
            task, user, team = self._task(session, {"research": True})
            agent, old_version = self._agent_version(session, user, team, ["writing"])
            _, latest_version = self._agent_version(
                session, user, team, ["research"], version_number=2, agent=agent
            )

            assign_task(session, task)
            session.commit()

            self.assertEqual(task.assignment_status, "assigned")
            self.assertEqual(task.assigned_agent_version_id, latest_version.id)
            self.assertEqual(len(task.assignment_candidates), 1)
            self.assertNotEqual(task.assigned_agent_version_id, old_version.id)
            self.assertEqual(task.assignment_candidates[0]["matched_capabilities"], ["research"])
            self.assertEqual(
                task.assignment_rationale["selected_agent_version_id"], str(latest_version.id)
            )

    def test_no_eligible_agent_records_capability_rejection(self) -> None:
        with self.Session() as session:
            task, user, team = self._task(session, {"review": True})
            self._agent_version(session, user, team, ["writing"])

            assign_task(session, task)

            self.assertEqual(task.assignment_status, "no_eligible_agent")
            self.assertIsNone(task.assigned_agent_version_id)
            self.assertIn("missing_capability:review", task.assignment_candidates[0]["rejection_reasons"])

    def test_policy_hold_blocks_otherwise_matching_candidate(self) -> None:
        with self.Session() as session:
            task, user, team = self._task(session, {"writing": True})
            agent, _ = self._agent_version(session, user, team, ["writing"])
            session.add(Policy(scope_type="agent", scope_id=agent.id, decision="deny", rule={}))
            session.flush()

            assign_task(session, task)

            self.assertEqual(task.assignment_status, "blocked")
            self.assertTrue(
                any(reason.startswith("agent_policy_deny:") for reason in task.assignment_candidates[0]["rejection_reasons"])
            )

    def test_exhausted_hard_stop_budget_blocks_assignment(self) -> None:
        with self.Session() as session:
            task, user, team = self._task(session, {"research": True})
            agent = Agent(team_id=team.id, created_by=user.id, name=f"Budget Agent {uuid.uuid4()}")
            session.add(agent)
            session.flush()
            budget = Budget(
                agent_id=agent.id,
                currency="USD",
                amount_minor_units=100,
                enforcement_mode="hard_stop",
            )
            session.add(budget)
            session.flush()
            self._agent_version(
                session, user, team, ["research"], default_budget_id=budget.id, agent=agent
            )
            session.add(
                CostLedgerEntry(
                    budget_id=budget.id,
                    action_type="model_call",
                    reserved_amount_minor_units=100,
                    currency="USD",
                    is_zero_cost=False,
                    status="reserved",
                )
            )
            session.flush()

            assign_task(session, task)

            self.assertEqual(task.assignment_status, "blocked")
            self.assertIn(f"budget_exhausted:{budget.id}", task.assignment_candidates[0]["rejection_reasons"])


if __name__ == "__main__":
    unittest.main()
