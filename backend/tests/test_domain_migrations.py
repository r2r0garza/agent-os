from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

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
    ModelProfile,
    Project,
    Run,
    Skill,
    SkillVersion,
    Task,
    TaskDependency,
    Team,
    TeamMembership,
    User,
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
        raise unittest.SkipTest(
            f"PostgreSQL is not reachable at {TEST_DATABASE_URL!r}; set "
            "AGENTIC_OS_DATABASE_URL to run domain migration tests: "
            f"{error}"
        )
    _apply_migrations_from_zero(TEST_DATABASE_URL)


class DomainMigrationTests(unittest.TestCase):
    """Proves migrations apply cleanly and the foundation domain schema
    supports the relational current-state + append-only audit shape
    required by exit criterion 1."""

    def setUp(self) -> None:
        self.engine = create_database_engine(TEST_DATABASE_URL)
        self.Session = session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_all_foundation_tables_exist(self) -> None:
        expected_tables = {
            "teams",
            "users",
            "team_memberships",
            "projects",
            "project_members",
            "goals",
            "tasks",
            "task_dependencies",
            "runs",
            "agents",
            "agent_versions",
            "skills",
            "skill_versions",
            "mcp_servers",
            "mcp_server_versions",
            "model_profiles",
            "policies",
            "budgets",
            "cost_ledger_entries",
            "artifacts",
            "artifact_versions",
            "audit_events",
            "workspace_resources",
            "workspace_resource_leases",
            "workspace_promotions",
        }
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
            )
            actual_tables = {row[0] for row in rows}
        missing = expected_tables - actual_tables
        self.assertFalse(missing, f"migrations are missing required tables: {missing}")

    def test_domain_records_are_non_singleton_with_stable_ids(self) -> None:
        with self.Session() as session:
            team_a = Team(name="Alpha Team")
            team_b = Team(name="Beta Team")
            session.add_all([team_a, team_b])
            session.commit()

            self.assertNotEqual(team_a.id, team_b.id)
            self.assertIsInstance(team_a.id, uuid.UUID)

            count = session.execute(text("SELECT count(*) FROM teams")).scalar_one()
            self.assertGreaterEqual(count, 2)

    def test_full_project_goal_task_run_lifecycle_with_audit_trail(self) -> None:
        with self.Session() as session:
            team = Team(name="Foundation Team")
            session.add(team)
            session.flush()

            user = User(email=f"operator-{uuid.uuid4()}@example.test", display_name="Operator")
            session.add(user)
            session.flush()

            session.add(TeamMembership(team_id=team.id, user_id=user.id))

            project = Project(team_id=team.id, created_by=user.id, name="Foundation Project")
            session.add(project)
            session.flush()

            goal = Goal(
                project_id=project.id,
                created_by=user.id,
                title="Ship the foundation slice",
                status="draft",
            )
            session.add(goal)
            session.flush()

            model_profile = ModelProfile(
                team_id=team.id,
                created_by=user.id,
                name="primary-openai-compatible",
                base_url="https://example.test/v1",
                model_identifier="test-model",
                api_key_ciphertext="ciphertext",
            )
            session.add(model_profile)
            session.flush()

            agent = Agent(team_id=team.id, created_by=user.id, name="Foundation Agent")
            session.add(agent)
            session.flush()

            budget = Budget(
                agent_id=agent.id,
                currency="USD",
                amount_minor_units=10_00,
                enforcement_mode="hard_stop",
            )
            session.add(budget)
            session.flush()

            agent_version = AgentVersion(
                agent_id=agent.id,
                version_number=1,
                capability_manifest={"capabilities": ["test"]},
                model_profile_id=model_profile.id,
                default_budget_id=budget.id,
            )
            session.add(agent_version)
            session.flush()

            skill = Skill(team_id=team.id, created_by=user.id, name="Test Skill")
            session.add(skill)
            session.flush()
            session.add(SkillVersion(skill_id=skill.id, version_number=1, content_ref="skills/test/v1"))

            mcp_server = McpServer(team_id=team.id, created_by=user.id, name="Test MCP Server")
            session.add(mcp_server)
            session.flush()
            session.add(
                McpServerVersion(
                    mcp_server_id=mcp_server.id,
                    version_number=1,
                    connection_config={"tools": ["echo"]},
                )
            )

            task = Task(goal_id=goal.id, title="Run the governed task", status="pending")
            session.add(task)
            session.flush()

            session.add(
                AuditEvent(
                    project_id=project.id,
                    goal_id=goal.id,
                    task_id=task.id,
                    event_type="task.created",
                    payload={"status": "pending"},
                )
            )

            # Current-state transition + append-only audit event committed together.
            task.status = "running"
            session.add(
                AuditEvent(
                    project_id=project.id,
                    goal_id=goal.id,
                    task_id=task.id,
                    event_type="task.status_changed",
                    payload={"from": "pending", "to": "running"},
                )
            )
            session.commit()

            run = Run(
                task_id=task.id,
                attempt_number=1,
                idempotency_key=f"{task.id}:1",
                lease_token=1,
                agent_version_id=agent_version.id,
                status="running",
            )
            session.add(run)
            session.flush()

            # A non-chargeable MCP tool call still emits an explicit zero-cost ledger entry.
            session.add(
                CostLedgerEntry(
                    budget_id=budget.id,
                    run_id=run.id,
                    action_type="mcp_tool_call",
                    reserved_amount_minor_units=0,
                    actual_amount_minor_units=0,
                    currency="USD",
                    is_zero_cost=True,
                    status="reconciled",
                )
            )
            # A metered model call reserves pessimistic cost ahead of usage.
            session.add(
                CostLedgerEntry(
                    budget_id=budget.id,
                    run_id=run.id,
                    action_type="model_call",
                    reserved_amount_minor_units=500,
                    currency="USD",
                    is_zero_cost=False,
                    status="reserved",
                )
            )

            artifact = Artifact(project_id=project.id, goal_id=goal.id, task_id=task.id, run_id=run.id, name="result.md")
            session.add(artifact)
            session.flush()
            artifact_v1 = ArtifactVersion(
                artifact_id=artifact.id,
                version_number=1,
                content_hash="sha256:" + "0" * 64,
                storage_ref="local://artifacts/result.md.v1",
            )
            session.add(artifact_v1)
            session.flush()

            run.status = "completed"
            session.add(
                AuditEvent(
                    project_id=project.id,
                    goal_id=goal.id,
                    task_id=task.id,
                    run_id=run.id,
                    event_type="run.completed",
                    payload={"artifact_id": str(artifact.id)},
                )
            )
            session.commit()

            self.assertEqual(task.status, "running")
            self.assertEqual(run.status, "completed")

            ledger_rows = session.execute(
                text("SELECT is_zero_cost, status FROM cost_ledger_entries WHERE run_id = :run_id ORDER BY is_zero_cost"),
                {"run_id": run.id},
            ).all()
            self.assertEqual(len(ledger_rows), 2)
            self.assertTrue(any(row.is_zero_cost for row in ledger_rows))
            self.assertTrue(any(not row.is_zero_cost for row in ledger_rows))

            audit_rows = session.execute(
                text(
                    "SELECT event_type, sequence_number FROM audit_events "
                    "WHERE task_id = :task_id ORDER BY sequence_number"
                ),
                {"task_id": task.id},
            ).all()
            self.assertEqual(
                [row.event_type for row in audit_rows],
                ["task.created", "task.status_changed", "run.completed"],
            )
            sequence_numbers = [row.sequence_number for row in audit_rows]
            self.assertEqual(sequence_numbers, sorted(sequence_numbers))

    def test_budget_amount_check_constraint_rejects_negative_amount(self) -> None:
        with self.Session() as session:
            team = Team(name="Constraint Team")
            session.add(team)
            session.flush()
            user = User(email=f"budget-{uuid.uuid4()}@example.test", display_name="Budget Owner")
            session.add(user)
            session.flush()
            agent = Agent(team_id=team.id, created_by=user.id, name="Constraint Agent")
            session.add(agent)
            session.flush()

            session.add(
                Budget(agent_id=agent.id, currency="USD", amount_minor_units=-1, enforcement_mode="warning")
            )
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_task_dependency_rejects_self_reference(self) -> None:
        with self.Session() as session:
            team = Team(name="Dependency Team")
            session.add(team)
            session.flush()
            user = User(email=f"dep-{uuid.uuid4()}@example.test", display_name="Dependency Owner")
            session.add(user)
            session.flush()
            project = Project(team_id=team.id, created_by=user.id, name="Dependency Project")
            session.add(project)
            session.flush()
            goal = Goal(project_id=project.id, created_by=user.id, title="Dependency Goal")
            session.add(goal)
            session.flush()
            task = Task(goal_id=goal.id, title="Self referencing task")
            session.add(task)
            session.flush()

            session.add(TaskDependency(task_id=task.id, depends_on_task_id=task.id))
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_mcp_server_requires_team_or_project_scope(self) -> None:
        with self.Session() as session:
            user = User(email=f"mcp-{uuid.uuid4()}@example.test", display_name="MCP Owner")
            session.add(user)
            session.flush()

            session.add(McpServer(created_by=user.id, name="Unscoped MCP server"))
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_team_membership_uniqueness_is_enforced(self) -> None:
        with self.Session() as session:
            team = Team(name="Unique Team")
            session.add(team)
            session.flush()
            user = User(email=f"member-{uuid.uuid4()}@example.test", display_name="Member")
            session.add(user)
            session.flush()

            session.add(TeamMembership(team_id=team.id, user_id=user.id))
            session.commit()

            session.add(TeamMembership(team_id=team.id, user_id=user.id))
            with self.assertRaises(IntegrityError):
                session.commit()


if __name__ == "__main__":
    unittest.main()
