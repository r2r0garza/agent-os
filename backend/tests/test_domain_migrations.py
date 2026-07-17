from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import (
    AdminOverride,
    Agent,
    AgentVersion,
    AgentVersionMcpServer,
    AgentVersionPolicySet,
    AgentVersionSkill,
    ApprovalDecisionRecord,
    ApprovalModeConfiguration,
    ApprovalRequest,
    Artifact,
    ArtifactVersion,
    AuditEvent,
    Budget,
    BudgetReservation,
    Credential,
    CostLedgerEntry,
    Goal,
    GoalLifecycleCommand,
    GoalLifecycleEvent,
    GoalSteeringRequest,
    McpServer,
    McpServerVersion,
    ModelProfile,
    ModelProfileVersion,
    ObservabilityRecord,
    PolicySet,
    PolicySetVersion,
    Project,
    Run,
    RunConfigurationSnapshot,
    Skill,
    SkillVersion,
    Task,
    TaskDependency,
    TaskGraphRevision,
    TaskGraphRevisionTask,
    Team,
    TeamMembership,
    TelemetryExportAttempt,
    TelemetryExportSetting,
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
            "goal_lifecycle_commands",
            "goal_lifecycle_events",
            "goal_steering_requests",
            "tasks",
            "task_dependencies",
            "task_graph_revisions",
            "task_graph_revision_tasks",
            "runs",
            "agents",
            "agent_versions",
            "skills",
            "skill_versions",
            "mcp_servers",
            "mcp_server_versions",
            "model_profiles",
            "model_profile_probes",
            "policies",
            "budgets",
            "cost_ledger_entries",
            "artifacts",
            "artifact_blobs",
            "artifact_versions",
            "audit_events",
            "workspace_resources",
            "workspace_resource_leases",
            "workspace_promotions",
            "credentials",
            "model_profile_versions",
            "agent_version_skills",
            "agent_version_mcp_servers",
            "policy_sets",
            "policy_set_versions",
            "agent_version_policy_sets",
            "run_configuration_snapshots",
            "approval_mode_configurations",
            "approval_requests",
            "approval_decisions",
            "admin_overrides",
            "budget_reservations",
            "observability_records",
            "telemetry_export_attempts",
            "telemetry_export_settings",
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

    def test_goal_controls_and_graph_revisions_survive_session_restart(self) -> None:
        now = datetime.now(UTC)
        with self.Session() as session:
            team = Team(name="Lifecycle Team")
            operator = User(
                email=f"lifecycle-{uuid.uuid4()}@example.test",
                display_name="Lifecycle Operator",
            )
            session.add_all([team, operator])
            session.flush()
            session.add(TeamMembership(team_id=team.id, user_id=operator.id))
            project = Project(
                team_id=team.id,
                created_by=operator.id,
                name="Lifecycle Project",
            )
            session.add(project)
            session.flush()
            goal = Goal(
                project_id=project.id,
                created_by=operator.id,
                title="Durably steer this goal",
                status="active",
                active_graph_revision_number=1,
            )
            session.add(goal)
            session.flush()

            original_task = Task(
                goal_id=goal.id,
                created_by=operator.id,
                title="Original unfinished task",
                status="running",
            )
            session.add(original_task)
            session.flush()
            initial_revision = TaskGraphRevision(
                goal_id=goal.id,
                created_by=operator.id,
                revision_number=1,
                graph_snapshot={"tasks": [str(original_task.id)], "dependencies": []},
            )
            session.add(initial_revision)
            session.flush()
            session.add(
                TaskGraphRevisionTask(
                    revision_id=initial_revision.id,
                    task_id=original_task.id,
                    change_type="added",
                    task_snapshot={
                        "title": original_task.title,
                        "status": original_task.status,
                    },
                )
            )

            model_profile = ModelProfile(
                team_id=team.id,
                created_by=operator.id,
                name="lifecycle-model",
                base_url="https://example.test/v1",
                model_identifier="test-model",
                api_key_ciphertext="encrypted",
            )
            agent = Agent(
                team_id=team.id,
                created_by=operator.id,
                name="Lifecycle Agent",
            )
            session.add_all([model_profile, agent])
            session.flush()
            agent_version = AgentVersion(
                agent_id=agent.id,
                version_number=1,
                capability_manifest={"capabilities": ["lifecycle"]},
                model_profile_id=model_profile.id,
            )
            session.add(agent_version)
            session.flush()
            original_run = Run(
                task_id=original_task.id,
                attempt_number=1,
                idempotency_key=f"{original_task.id}:1",
                lease_token=1,
                agent_version_id=agent_version.id,
                status="running",
            )
            session.add(original_run)
            session.flush()
            original_artifact = Artifact(
                project_id=project.id,
                goal_id=goal.id,
                task_id=original_task.id,
                run_id=original_run.id,
                created_by=operator.id,
                name="partial.md",
            )
            session.add(original_artifact)
            session.flush()
            original_artifact_version = ArtifactVersion(
                artifact_id=original_artifact.id,
                version_number=1,
                content_hash="sha256:" + "7" * 64,
                storage_ref="local://partial.md",
            )
            session.add(original_artifact_version)

            cancel_command = GoalLifecycleCommand(
                goal_id=goal.id,
                requested_by=operator.id,
                command_type="cancel",
                status="applied",
                idempotency_key=f"{goal.id}:cancel:1",
                reason="Operator stopped the goal",
                prior_goal_status="active",
                target_goal_status="cancelled",
                cancellation_grace_expires_at=now + timedelta(seconds=30),
                forced_termination_requested_at=now + timedelta(seconds=31),
                forced_termination_completed_at=now + timedelta(seconds=32),
                applied_at=now + timedelta(seconds=1),
                evidence={"authorization": "team_member", "safe_boundary": "checkpoint"},
            )
            session.add(cancel_command)
            session.flush()
            goal.control_version = 1
            goal.pending_control = "cancel"
            goal.control_requested_by = operator.id
            goal.control_requested_at = now
            goal.cancellation_grace_expires_at = cancel_command.cancellation_grace_expires_at
            goal.forced_termination_requested_at = (
                cancel_command.forced_termination_requested_at
            )
            goal.forced_termination_completed_at = (
                cancel_command.forced_termination_completed_at
            )
            session.add(
                GoalLifecycleEvent(
                    goal_id=goal.id,
                    actor_id=operator.id,
                    lifecycle_command_id=cancel_command.id,
                    event_type="goal.cancel.requested",
                    prior_goal_status="active",
                    resulting_goal_status="active",
                    payload={"control_version": 1},
                )
            )

            steering_request = GoalSteeringRequest(
                goal_id=goal.id,
                requested_by=operator.id,
                status="applied",
                idempotency_key=f"{goal.id}:steer:1",
                instruction="Replace the unfinished task with a safer plan",
                base_revision_number=1,
                applied_revision_number=2,
                resolved_at=now + timedelta(seconds=2),
                evidence={"authorization": "team_member"},
            )
            session.add(steering_request)
            session.flush()
            replacement_task = Task(
                goal_id=goal.id,
                created_by=operator.id,
                title="Replacement planned task",
                status="pending",
            )
            session.add(replacement_task)
            session.flush()
            revised_graph = TaskGraphRevision(
                goal_id=goal.id,
                created_by=operator.id,
                steering_request_id=steering_request.id,
                revision_number=2,
                parent_revision_number=1,
                change_summary="Supersede unfinished work without rewriting its attempt",
                graph_snapshot={
                    "tasks": [str(replacement_task.id)],
                    "dependencies": [],
                },
                assignment_evidence={"state": "unassigned"},
                policy_context={"policy_version_ids": []},
                budget_context={"inherited": True},
            )
            session.add(revised_graph)
            session.flush()
            session.add(
                TaskGraphRevisionTask(
                    revision_id=revised_graph.id,
                    task_id=replacement_task.id,
                    change_type="revised",
                    supersedes_task_id=original_task.id,
                    task_snapshot={
                        "title": replacement_task.title,
                        "status": replacement_task.status,
                    },
                )
            )
            goal.active_graph_revision_number = 2
            session.add_all(
                [
                    GoalLifecycleEvent(
                        goal_id=goal.id,
                        actor_id=operator.id,
                        steering_request_id=steering_request.id,
                        event_type="goal.steering.applied",
                        prior_goal_status="active",
                        resulting_goal_status="active",
                        payload={"revision_number": 2},
                    ),
                    GoalLifecycleEvent(
                        goal_id=goal.id,
                        actor_id=operator.id,
                        lifecycle_command_id=cancel_command.id,
                        graph_revision_id=revised_graph.id,
                        event_type="goal.cancel.forced_termination_completed",
                        prior_goal_status="active",
                        resulting_goal_status="cancelled",
                        payload={"control_version": 1},
                    ),
                ]
            )
            goal.status = "cancelled"
            goal.pending_control = None
            session.commit()

            goal_id = goal.id
            operator_id = operator.id
            original_task_id = original_task.id
            original_run_id = original_run.id
            original_artifact_version_id = original_artifact_version.id

        # A new session represents the durable restart/recovery boundary.
        with self.Session() as session:
            persisted_goal = session.get(Goal, goal_id)
            self.assertIsNotNone(persisted_goal)
            self.assertEqual(persisted_goal.status, "cancelled")
            self.assertEqual(persisted_goal.control_version, 1)
            self.assertEqual(persisted_goal.active_graph_revision_number, 2)
            self.assertIsNotNone(persisted_goal.forced_termination_completed_at)

            revisions = session.execute(
                text(
                    "SELECT revision_number FROM task_graph_revisions "
                    "WHERE goal_id = :goal_id ORDER BY revision_number"
                ),
                {"goal_id": goal_id},
            ).scalars().all()
            self.assertEqual(revisions, [1, 2])

            event_rows = session.execute(
                text(
                    "SELECT actor_id, event_type, sequence_number "
                    "FROM goal_lifecycle_events WHERE goal_id = :goal_id "
                    "ORDER BY sequence_number"
                ),
                {"goal_id": goal_id},
            ).all()
            self.assertEqual(
                [row.event_type for row in event_rows],
                [
                    "goal.cancel.requested",
                    "goal.steering.applied",
                    "goal.cancel.forced_termination_completed",
                ],
            )
            self.assertTrue(all(row.actor_id == operator_id for row in event_rows))
            self.assertEqual(
                [row.sequence_number for row in event_rows],
                sorted(row.sequence_number for row in event_rows),
            )

            # Superseding unfinished work records a new graph entry; it never
            # rewrites the original task, attempt, or immutable artifact version.
            self.assertEqual(session.get(Task, original_task_id).status, "running")
            self.assertEqual(session.get(Run, original_run_id).attempt_number, 1)
            self.assertEqual(
                session.get(ArtifactVersion, original_artifact_version_id).content_hash,
                "sha256:" + "7" * 64,
            )

    def test_goal_control_and_steering_idempotency_is_enforced(self) -> None:
        with self.Session() as session:
            team = Team(name="Lifecycle Idempotency Team")
            operator = User(
                email=f"lifecycle-idempotency-{uuid.uuid4()}@example.test",
                display_name="Lifecycle Operator",
            )
            session.add_all([team, operator])
            session.flush()
            project = Project(
                team_id=team.id,
                created_by=operator.id,
                name="Lifecycle Idempotency Project",
            )
            session.add(project)
            session.flush()
            goal = Goal(
                project_id=project.id,
                created_by=operator.id,
                title="Idempotent controls",
                status="active",
            )
            session.add(goal)
            session.flush()
            session.add_all(
                [
                    GoalLifecycleCommand(
                        goal_id=goal.id,
                        requested_by=operator.id,
                        command_type="pause",
                        idempotency_key="same-command",
                    ),
                    GoalLifecycleCommand(
                        goal_id=goal.id,
                        requested_by=operator.id,
                        command_type="pause",
                        idempotency_key="same-command",
                    ),
                ]
            )
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_observability_records_link_canonical_evidence_and_export_states(self) -> None:
        with self.Session() as session:
            team = Team(name="Observability Team")
            user = User(
                email=f"observability-{uuid.uuid4()}@example.test",
                display_name="Observability Operator",
            )
            session.add_all([team, user])
            session.flush()
            session.add(TeamMembership(team_id=team.id, user_id=user.id))

            project = Project(team_id=team.id, created_by=user.id, name="Observed Project")
            session.add(project)
            session.flush()
            goal = Goal(project_id=project.id, created_by=user.id, title="Observe a run")
            session.add(goal)
            session.flush()

            model_profile = ModelProfile(
                team_id=team.id,
                created_by=user.id,
                name="observability-model",
                base_url="https://example.test/v1",
                model_identifier="test-model",
                api_key_ciphertext="encrypted",
            )
            agent = Agent(team_id=team.id, created_by=user.id, name="Observed Agent")
            session.add_all([model_profile, agent])
            session.flush()
            agent_version = AgentVersion(
                agent_id=agent.id,
                version_number=1,
                capability_manifest={"capabilities": ["observe"]},
                model_profile_id=model_profile.id,
            )
            session.add(agent_version)
            session.flush()

            task = Task(goal_id=goal.id, title="Emit correlated evidence")
            session.add(task)
            session.flush()
            run = Run(
                task_id=task.id,
                attempt_number=1,
                idempotency_key=f"{task.id}:1",
                lease_token=1,
                agent_version_id=agent_version.id,
            )
            session.add(run)
            session.flush()

            audit_event = AuditEvent(
                project_id=project.id,
                goal_id=goal.id,
                task_id=task.id,
                run_id=run.id,
                event_type="model.call.completed",
                payload={"redacted": True},
            )
            ledger_entry = CostLedgerEntry(
                run_id=run.id,
                team_id=team.id,
                project_id=project.id,
                goal_id=goal.id,
                task_id=task.id,
                agent_version_id=agent_version.id,
                actor_id=user.id,
                action_type="model_call",
                currency="USD",
                is_zero_cost=True,
                status="reconciled",
            )
            approval = ApprovalRequest(
                team_id=team.id,
                project_id=project.id,
                goal_id=goal.id,
                task_id=task.id,
                run_id=run.id,
                agent_version_id=agent_version.id,
                requested_by=user.id,
                mode="auto",
                action_type="model_call",
            )
            artifact = Artifact(
                project_id=project.id,
                goal_id=goal.id,
                task_id=task.id,
                run_id=run.id,
                created_by=user.id,
                name="observed-result.md",
            )
            session.add_all([audit_event, ledger_entry, approval, artifact])
            session.flush()
            artifact_version = ArtifactVersion(
                artifact_id=artifact.id,
                version_number=1,
                content_hash="sha256:" + "1" * 64,
                storage_ref="local://observed-result.md",
            )
            session.add(artifact_version)
            session.flush()

            export_setting = TelemetryExportSetting(
                team_id=team.id,
                project_id=project.id,
                created_by=user.id,
                exporter_type="opentelemetry",
                enabled=True,
                endpoint_reference="environment:OTEL_EXPORTER_OTLP_ENDPOINT",
                capture_prompts=False,
                capture_outputs=False,
                redaction_policy_evidence={"policy_version": "redaction-v1", "secrets": "removed"},
                configuration_evidence={"credential_source": "environment", "credential_value": "omitted"},
            )
            session.add(export_setting)
            session.flush()

            correlation_id = uuid.uuid4()
            trace_id = "a" * 32
            record = ObservabilityRecord(
                correlation_id=correlation_id,
                request_id=uuid.uuid4(),
                trace_id=trace_id,
                span_id="b" * 16,
                event_kind="model_call",
                operation_name="worker.model_call",
                status="completed",
                team_id=team.id,
                user_id=user.id,
                project_id=project.id,
                goal_id=goal.id,
                task_id=task.id,
                run_id=run.id,
                audit_event_id=audit_event.id,
                cost_ledger_entry_id=ledger_entry.id,
                approval_request_id=approval.id,
                artifact_id=artifact.id,
                artifact_version_id=artifact_version.id,
                model_call_id=uuid.uuid4(),
                attributes={"model": "test-model", "payload_capture": "omitted"},
                capture_policy_evidence={"prompts": False, "outputs": False},
                redaction_evidence={"fields_redacted": ["authorization", "api_key"]},
            )
            checkpoint_record = ObservabilityRecord(
                correlation_id=correlation_id,
                trace_id=trace_id,
                span_id="c" * 16,
                parent_span_id="b" * 16,
                event_kind="checkpoint",
                operation_name="langgraph.checkpoint",
                team_id=team.id,
                project_id=project.id,
                goal_id=goal.id,
                task_id=task.id,
                run_id=run.id,
                checkpoint_id=uuid.uuid4(),
            )
            session.add_all([record, checkpoint_record])
            session.flush()

            delivery_states = ("pending", "delivered", "dropped", "delayed", "disabled", "failed")
            session.add_all(
                [
                    TelemetryExportAttempt(
                        observability_record_id=record.id,
                        export_setting_id=export_setting.id,
                        destination="opentelemetry",
                        attempt_number=index,
                        status=status,
                        failure_code="export_unavailable" if status == "failed" else None,
                        delivery_evidence={"payload": "redacted", "state": status},
                    )
                    for index, status in enumerate(delivery_states, start=1)
                ]
            )
            session.commit()

            filters = {
                "run_id": run.id,
                "goal_id": goal.id,
                "project_id": project.id,
                "team_id": team.id,
                "correlation_id": correlation_id,
            }
            for column, value in filters.items():
                count = session.execute(
                    text(f"SELECT count(*) FROM observability_records WHERE {column} = :value"),
                    {"value": value},
                ).scalar_one()
                self.assertEqual(count, 2)

            trace_count = session.execute(
                text("SELECT count(*) FROM observability_records WHERE trace_id = :trace_id"),
                {"trace_id": trace_id},
            ).scalar_one()
            self.assertEqual(trace_count, 2)

            persisted_states = session.execute(
                text(
                    "SELECT status FROM telemetry_export_attempts "
                    "WHERE observability_record_id = :record_id ORDER BY attempt_number"
                ),
                {"record_id": record.id},
            ).scalars().all()
            self.assertEqual(persisted_states, list(delivery_states))
            self.assertNotIn("encrypted", str(record.attributes))
            self.assertNotIn("encrypted", str(export_setting.configuration_evidence))

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

    def test_approval_records_pin_configuration_and_policy_evidence_by_scope(self) -> None:
        with self.Session() as session:
            team = Team(name="Approval Evidence Team")
            operator = User(
                email=f"approval-operator-{uuid.uuid4()}@example.test",
                display_name="Approval Operator",
            )
            admin = User(
                email=f"approval-admin-{uuid.uuid4()}@example.test",
                display_name="Approval Admin",
                role="admin",
            )
            session.add_all([team, operator, admin])
            session.flush()
            session.add_all(
                [
                    TeamMembership(team_id=team.id, user_id=operator.id),
                    TeamMembership(team_id=team.id, user_id=admin.id),
                ]
            )
            project = Project(team_id=team.id, created_by=operator.id, name="Approval Project")
            session.add(project)
            session.flush()
            goal = Goal(project_id=project.id, created_by=operator.id, title="Approval Goal")
            session.add(goal)
            session.flush()
            agent = Agent(team_id=team.id, created_by=operator.id, name="Approval Agent")
            session.add(agent)
            session.flush()
            agent_version = AgentVersion(agent_id=agent.id, version_number=1, capability_manifest={})
            session.add(agent_version)
            session.flush()
            task = Task(
                goal_id=goal.id,
                title="Approval Task",
                assigned_agent_version_id=agent_version.id,
            )
            session.add(task)
            session.flush()
            run = Run(
                task_id=task.id,
                attempt_number=1,
                idempotency_key=f"{task.id}:approval:1",
                lease_token=1,
                agent_version_id=agent_version.id,
            )
            session.add(run)
            session.flush()

            policy_set = PolicySet(team_id=team.id, created_by=admin.id, name="Approval policy")
            session.add(policy_set)
            session.flush()
            policy_version = PolicySetVersion(
                policy_set_id=policy_set.id,
                version_number=1,
                rules=[{"action": "tool.publish", "decision": "approval_required"}],
            )
            session.add(policy_version)
            session.flush()

            config_v1 = ApprovalModeConfiguration(
                team_id=team.id,
                project_id=project.id,
                goal_id=goal.id,
                configured_by=operator.id,
                version_number=1,
                mode="consequential",
                consequential_action_types=["tool.publish"],
                context={"source": "goal"},
            )
            session.add(config_v1)
            session.flush()
            expires_at = datetime.now(UTC) + timedelta(hours=1)
            request = ApprovalRequest(
                team_id=team.id,
                project_id=project.id,
                goal_id=goal.id,
                task_id=task.id,
                run_id=run.id,
                agent_version_id=agent_version.id,
                configuration_id=config_v1.id,
                requested_by=operator.id,
                mode=config_v1.mode,
                action_type="tool.publish",
                action_preview={"destination": "staging", "credential": "[REDACTED]"},
                policy_version_ids=[str(policy_version.id)],
                policy_evidence={"decision": "approval_required", "secret": "[REDACTED]"},
                expires_at=expires_at,
            )
            session.add(request)
            session.flush()
            request.status = "approved"
            request.resolved_at = datetime.now(UTC)
            session.add(
                ApprovalDecisionRecord(
                    approval_request_id=request.id,
                    decision="approved",
                    actor_id=operator.id,
                    reason="Reviewed destination and payload",
                    context={"channel": "operator_queue"},
                    evaluated_policy_version_ids=[str(policy_version.id)],
                )
            )
            override = AdminOverride(
                team_id=team.id,
                project_id=project.id,
                goal_id=goal.id,
                task_id=task.id,
                run_id=run.id,
                created_by=admin.id,
                scope_type="run",
                scope_id=run.id,
                reason="Bounded incident response override",
                starts_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
                evaluated_policy_version_ids=[str(policy_version.id)],
                context={"ticket": "INC-42"},
            )
            session.add(override)

            # A later edit creates a new configuration record. Existing requests
            # continue to point at the version they evaluated before interruption.
            config_v2 = ApprovalModeConfiguration(
                team_id=team.id,
                project_id=project.id,
                goal_id=goal.id,
                configured_by=admin.id,
                version_number=2,
                mode="every_tool_call",
                consequential_action_types=[],
                context={"source": "admin_edit"},
            )
            session.add(config_v2)
            session.commit()
            request_id = request.id

        with self.Session() as session:
            scoped = session.execute(
                text(
                    "SELECT id, configuration_id, run_id, action_preview, policy_evidence "
                    "FROM approval_requests "
                    "WHERE team_id = :team_id AND project_id = :project_id AND run_id = :run_id"
                ),
                {"team_id": team.id, "project_id": project.id, "run_id": run.id},
            ).one()
            self.assertEqual(scoped.id, request_id)
            self.assertEqual(scoped.configuration_id, config_v1.id)
            self.assertEqual(scoped.action_preview["credential"], "[REDACTED]")
            self.assertEqual(scoped.policy_evidence["secret"], "[REDACTED]")
            self.assertEqual(
                session.execute(
                    text("SELECT count(*) FROM approval_mode_configurations WHERE goal_id = :goal_id"),
                    {"goal_id": goal.id},
                ).scalar_one(),
                2,
            )
            decision = session.execute(
                text(
                    "SELECT actor_id, reason, evaluated_policy_version_ids "
                    "FROM approval_decisions WHERE approval_request_id = :request_id"
                ),
                {"request_id": request_id},
            ).one()
            self.assertEqual(decision.actor_id, operator.id)
            self.assertEqual(decision.evaluated_policy_version_ids, [str(policy_version.id)])
            persisted_override = session.execute(
                text("SELECT created_by, reason, scope_id FROM admin_overrides WHERE run_id = :run_id"),
                {"run_id": run.id},
            ).one()
            self.assertEqual(persisted_override.created_by, admin.id)
            self.assertEqual(persisted_override.scope_id, run.id)

    def test_budget_reservations_and_reconciled_ledger_are_durable_integer_evidence(self) -> None:
        with self.Session() as session:
            team = Team(name="Budget Evidence Team")
            user = User(
                email=f"budget-evidence-{uuid.uuid4()}@example.test",
                display_name="Budget Operator",
            )
            session.add_all([team, user])
            session.flush()
            project = Project(team_id=team.id, created_by=user.id, name="Budget Project")
            session.add(project)
            session.flush()
            goal = Goal(project_id=project.id, created_by=user.id, title="Budget Goal")
            session.add(goal)
            session.flush()
            agent = Agent(team_id=team.id, created_by=user.id, name="Budget Agent")
            session.add(agent)
            session.flush()
            budget = Budget(
                agent_id=agent.id,
                currency="USD",
                amount_minor_units=10_000,
                enforcement_mode="hard_stop",
                warning_threshold_percent=80,
            )
            session.add(budget)
            session.flush()
            agent_version = AgentVersion(
                agent_id=agent.id,
                version_number=1,
                capability_manifest={},
                default_budget_id=budget.id,
            )
            session.add(agent_version)
            session.flush()
            task = Task(goal_id=goal.id, title="Budget Task", assigned_agent_version_id=agent_version.id)
            session.add(task)
            session.flush()
            run = Run(
                task_id=task.id,
                attempt_number=1,
                idempotency_key=f"{task.id}:budget:1",
                lease_token=1,
                agent_version_id=agent_version.id,
            )
            session.add(run)
            session.flush()

            reservation = BudgetReservation(
                budget_id=budget.id,
                team_id=team.id,
                project_id=project.id,
                goal_id=goal.id,
                task_id=task.id,
                run_id=run.id,
                agent_version_id=agent_version.id,
                requested_by=user.id,
                action_type="model_call",
                amount_minor_units=300,
                currency="USD",
                status="reconciled",
                warning_triggered=True,
                pricing_evidence={"pricing_version": "2026-07", "maximum_tokens": 1000},
                policy_version_ids=[str(uuid.uuid4())],
                reconciled_at=datetime.now(UTC),
            )
            session.add(reservation)
            session.flush()
            ledger = CostLedgerEntry(
                budget_id=budget.id,
                run_id=run.id,
                reservation_id=reservation.id,
                team_id=team.id,
                project_id=project.id,
                goal_id=goal.id,
                task_id=task.id,
                agent_version_id=agent_version.id,
                actor_id=user.id,
                action_type="model_call",
                reserved_amount_minor_units=300,
                actual_amount_minor_units=275,
                currency="USD",
                warning_triggered=True,
                evidence={"provider_usage": {"input_tokens": 250, "output_tokens": 50}},
                status="reconciled",
            )
            session.add(ledger)
            session.commit()
            reservation_id = reservation.id
            ledger_id = ledger.id

            # Later budget configuration edits do not rewrite historical money evidence.
            budget.amount_minor_units = 20_000
            budget.warning_threshold_percent = 90
            session.commit()

        with self.Session() as session:
            scoped_reservation = session.execute(
                text(
                    "SELECT id, amount_minor_units, status FROM budget_reservations "
                    "WHERE team_id = :team_id AND project_id = :project_id AND run_id = :run_id"
                ),
                {"team_id": team.id, "project_id": project.id, "run_id": run.id},
            ).one()
            self.assertEqual(scoped_reservation.id, reservation_id)
            self.assertEqual(scoped_reservation.amount_minor_units, 300)
            scoped_ledger = session.execute(
                text(
                    "SELECT id, reserved_amount_minor_units, actual_amount_minor_units, actor_id "
                    "FROM cost_ledger_entries "
                    "WHERE team_id = :team_id AND project_id = :project_id AND run_id = :run_id"
                ),
                {"team_id": team.id, "project_id": project.id, "run_id": run.id},
            ).one()
            self.assertEqual(scoped_ledger.id, ledger_id)
            self.assertEqual(scoped_ledger.reserved_amount_minor_units, 300)
            self.assertEqual(scoped_ledger.actual_amount_minor_units, 275)
            self.assertIsInstance(scoped_ledger.actual_amount_minor_units, int)
            self.assertEqual(scoped_ledger.actor_id, user.id)

            invalid = BudgetReservation(
                budget_id=budget.id,
                team_id=team.id,
                project_id=project.id,
                goal_id=goal.id,
                task_id=task.id,
                run_id=run.id,
                agent_version_id=agent_version.id,
                action_type="unpriced_tool",
                amount_minor_units=1,
                currency="USD",
                is_unpriced=True,
                status="rejected",
            )
            session.add(invalid)
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

    def test_credential_redacted_metadata_excludes_secret_material(self) -> None:
        with self.Session() as session:
            team = Team(name="Credential Team")
            session.add(team)
            session.flush()
            user = User(email=f"cred-{uuid.uuid4()}@example.test", display_name="Credential Owner")
            session.add(user)
            session.flush()

            credential = Credential(
                team_id=team.id,
                created_by=user.id,
                name="Primary API Key",
                credential_type="api_key",
                encrypted_material="ciphertext-not-a-real-secret",
                metadata_={"provider": "openai-compatible"},
            )
            session.add(credential)
            session.commit()

            redacted = credential.redacted_metadata()
            self.assertNotIn("encrypted_material", redacted)
            self.assertTrue(redacted["configured"])
            self.assertEqual(redacted["name"], "Primary API Key")

    def test_credential_requires_exactly_one_owner_scope(self) -> None:
        with self.Session() as session:
            user = User(email=f"cred-scope-{uuid.uuid4()}@example.test", display_name="Scope Owner")
            session.add(user)
            session.flush()

            session.add(
                Credential(
                    created_by=user.id,
                    name="Unscoped credential",
                    credential_type="api_key",
                    encrypted_material="ciphertext",
                )
            )
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_run_configuration_snapshot_remains_immutable_after_later_edits(self) -> None:
        with self.Session() as session:
            team = Team(name="Snapshot Team")
            session.add(team)
            session.flush()
            user = User(email=f"snap-{uuid.uuid4()}@example.test", display_name="Snapshot Owner")
            session.add(user)
            session.flush()
            project = Project(team_id=team.id, created_by=user.id, name="Snapshot Project")
            session.add(project)
            session.flush()
            goal = Goal(project_id=project.id, created_by=user.id, title="Snapshot Goal")
            session.add(goal)
            session.flush()
            task = Task(goal_id=goal.id, title="Snapshot Task")
            session.add(task)
            session.flush()

            credential = Credential(
                team_id=team.id,
                created_by=user.id,
                name="Model credential",
                credential_type="api_key",
                encrypted_material="ciphertext",
            )
            session.add(credential)
            session.flush()

            model_profile = ModelProfile(
                team_id=team.id,
                created_by=user.id,
                name="snapshot-profile",
                base_url="https://example.test/v1",
                model_identifier="test-model",
                api_key_ciphertext="ciphertext",
            )
            session.add(model_profile)
            session.flush()

            profile_v1 = ModelProfileVersion(
                model_profile_id=model_profile.id,
                version_number=1,
                base_url="https://v1.example.test/v1",
                model_identifier="test-model-v1",
                credential_id=credential.id,
            )
            session.add(profile_v1)
            session.flush()

            agent = Agent(team_id=team.id, created_by=user.id, name="Snapshot Agent")
            session.add(agent)
            session.flush()
            budget = Budget(
                agent_id=agent.id, currency="USD", amount_minor_units=1000, enforcement_mode="hard_stop"
            )
            session.add(budget)
            session.flush()

            agent_version_1 = AgentVersion(
                agent_id=agent.id,
                version_number=1,
                capability_manifest={"capabilities": ["test"]},
                model_profile_id=model_profile.id,
                model_profile_version_id=profile_v1.id,
                default_budget_id=budget.id,
            )
            session.add(agent_version_1)
            session.flush()

            run = Run(
                task_id=task.id,
                attempt_number=1,
                idempotency_key=f"{task.id}:1",
                lease_token=1,
                agent_version_id=agent_version_1.id,
                status="running",
            )
            session.add(run)
            session.flush()

            snapshot = RunConfigurationSnapshot(
                run_id=run.id,
                team_id=team.id,
                project_id=project.id,
                agent_version_id=agent_version_1.id,
                model_profile_version_id=profile_v1.id,
                budget_id=budget.id,
                configuration={"base_url": profile_v1.base_url, "model_identifier": profile_v1.model_identifier},
            )
            session.add(snapshot)
            session.commit()
            snapshot_id = snapshot.id

            # Editing configuration afterwards must create a new version rather than
            # mutate the version the snapshot already pinned.
            profile_v2 = ModelProfileVersion(
                model_profile_id=model_profile.id,
                version_number=2,
                base_url="https://v2.example.test/v1",
                model_identifier="test-model-v2",
                credential_id=credential.id,
            )
            session.add(profile_v2)
            agent_version_2 = AgentVersion(
                agent_id=agent.id,
                version_number=2,
                capability_manifest={"capabilities": ["test", "more"]},
                model_profile_id=model_profile.id,
                model_profile_version_id=profile_v2.id,
                default_budget_id=budget.id,
            )
            session.add(agent_version_2)
            session.commit()

        with self.Session() as session:
            reloaded_snapshot = session.get(RunConfigurationSnapshot, snapshot_id)
            reloaded_profile_v1 = session.get(ModelProfileVersion, profile_v1.id)

            self.assertEqual(reloaded_snapshot.model_profile_version_id, profile_v1.id)
            self.assertEqual(reloaded_snapshot.agent_version_id, agent_version_1.id)
            self.assertEqual(reloaded_snapshot.configuration["base_url"], "https://v1.example.test/v1")
            self.assertEqual(reloaded_profile_v1.base_url, "https://v1.example.test/v1")

            profile_count = session.execute(
                text("SELECT count(*) FROM model_profile_versions WHERE model_profile_id = :id"),
                {"id": model_profile.id},
            ).scalar_one()
            self.assertEqual(profile_count, 2)

    def test_agent_version_skill_and_mcp_server_attachments_are_versioned(self) -> None:
        with self.Session() as session:
            team = Team(name="Attachment Team")
            session.add(team)
            session.flush()
            user = User(email=f"attach-{uuid.uuid4()}@example.test", display_name="Attachment Owner")
            session.add(user)
            session.flush()

            agent = Agent(team_id=team.id, created_by=user.id, name="Attachment Agent")
            session.add(agent)
            session.flush()
            agent_version = AgentVersion(agent_id=agent.id, version_number=1, capability_manifest={})
            session.add(agent_version)
            session.flush()

            skill = Skill(team_id=team.id, created_by=user.id, name="Attachment Skill")
            session.add(skill)
            session.flush()
            skill_version = SkillVersion(skill_id=skill.id, version_number=1, content_ref="skills/attach/v1")
            session.add(skill_version)
            session.flush()

            mcp_server = McpServer(team_id=team.id, created_by=user.id, name="Attachment MCP Server")
            session.add(mcp_server)
            session.flush()
            mcp_server_version = McpServerVersion(
                mcp_server_id=mcp_server.id, version_number=1, connection_config={"tools": ["echo"]}
            )
            session.add(mcp_server_version)
            session.flush()

            session.add(
                AgentVersionSkill(agent_version_id=agent_version.id, skill_version_id=skill_version.id)
            )
            session.add(
                AgentVersionMcpServer(
                    agent_version_id=agent_version.id, mcp_server_version_id=mcp_server_version.id
                )
            )
            session.commit()

            # Re-attaching the same skill version to the same agent version is rejected.
            session.add(
                AgentVersionSkill(agent_version_id=agent_version.id, skill_version_id=skill_version.id)
            )
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_policy_set_versions_govern_agent_versions(self) -> None:
        with self.Session() as session:
            team = Team(name="Policy Team")
            session.add(team)
            session.flush()
            user = User(email=f"policy-{uuid.uuid4()}@example.test", display_name="Policy Owner")
            session.add(user)
            session.flush()

            agent = Agent(team_id=team.id, created_by=user.id, name="Policy Agent")
            session.add(agent)
            session.flush()
            agent_version = AgentVersion(agent_id=agent.id, version_number=1, capability_manifest={})
            session.add(agent_version)
            session.flush()

            policy_set = PolicySet(team_id=team.id, created_by=user.id, name="Attachment Policy Set")
            session.add(policy_set)
            session.flush()
            policy_set_version = PolicySetVersion(
                policy_set_id=policy_set.id,
                version_number=1,
                rules=[{"scope": "tool", "decision": "allow"}],
            )
            session.add(policy_set_version)
            session.flush()

            session.add(
                AgentVersionPolicySet(
                    agent_version_id=agent_version.id, policy_set_version_id=policy_set_version.id
                )
            )
            session.commit()

            count = session.execute(
                text("SELECT count(*) FROM agent_version_policy_sets WHERE agent_version_id = :id"),
                {"id": agent_version.id},
            ).scalar_one()
            self.assertEqual(count, 1)

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
