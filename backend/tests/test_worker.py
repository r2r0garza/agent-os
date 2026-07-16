from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sqlalchemy import create_engine, select

from agentic_os.artifacts import (
    ArtifactContentUnavailableError,
    KnowledgeUnavailableError,
    LocalArtifactStorage,
    create_artifact_version,
    ingest_source_artifact,
)
from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import (
    Agent,
    AgentVersion,
    AgentVersionMcpServer,
    AgentVersionSkill,
    Artifact,
    ArtifactCitation,
    ArtifactVersion,
    AuditEvent,
    ApprovalModeConfiguration,
    ApprovalRequest,
    AdminOverride,
    Budget,
    BudgetReservation,
    CostLedgerEntry,
    Goal,
    McpServer,
    McpServerVersion,
    Policy,
    Project,
    Run,
    RunConfigurationSnapshot,
    Skill,
    SkillVersion,
    Task,
    Team,
    User,
)
from agentic_os.sandbox import runtime_available
from agentic_os.worker import claim_ready_task, run_task_worker_once
from agentic_os.worker.governance import (
    BudgetActionContext,
    BudgetExhaustedError,
    BudgetLimit,
    reserve_action_cost,
)
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

    def _build_ready_task(
        self,
        session,
        *,
        tools: tuple[str, ...] = ("echo",),
        sandbox: dict | None = None,
        tool_pricing: dict | None = None,
        budget_amount_minor_units: int = 10_00,
        budget_enforcement_mode: str = "hard_stop",
        budget_warning_threshold_percent: int | None = None,
    ) -> Task:
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
        echo_tool_descriptor: dict = {"name": "echo", "description": "Echo input"}
        if tool_pricing is not None:
            echo_tool_descriptor["pricing"] = tool_pricing
        mcp_server_version = McpServerVersion(
            mcp_server_id=mcp_server.id,
            version_number=1,
            connection_config={"tools": [echo_tool_descriptor]},
        )
        session.add(mcp_server_version)
        session.flush()

        agent = Agent(team_id=team.id, created_by=user.id, name="Worker Agent")
        session.add(agent)
        session.flush()

        budget = Budget(
            agent_id=agent.id,
            currency="USD",
            amount_minor_units=budget_amount_minor_units,
            enforcement_mode=budget_enforcement_mode,
            warning_threshold_percent=budget_warning_threshold_percent,
        )
        session.add(budget)
        session.flush()

        capability_manifest = {
            "skill_version_id": str(skill_version.id),
            "mcp_server_version_id": str(mcp_server_version.id),
            "enabled_tools": list(tools),
        }
        if sandbox is not None:
            capability_manifest["sandbox"] = sandbox

        agent_version = AgentVersion(
            agent_id=agent.id,
            version_number=1,
            capability_manifest=capability_manifest,
            model_profile_id=None,
            default_budget_id=budget.id,
        )
        session.add(agent_version)
        session.flush()
        session.add(
            AgentVersionSkill(
                agent_version_id=agent_version.id,
                skill_version_id=skill_version.id,
                attachment_config={},
            )
        )
        session.add(
            AgentVersionMcpServer(
                agent_version_id=agent_version.id,
                mcp_server_version_id=mcp_server_version.id,
                attachment_config={},
            )
        )
        session.flush()

        task = Task(
            goal_id=goal.id,
            title="Governed worker task",
            status="pending",
            assigned_agent_version_id=agent_version.id,
            assignment_status="assigned",
            assignment_rationale={"selected_agent_version_id": str(agent_version.id)},
        )
        session.add(task)
        session.flush()
        session.commit()
        return task

    def _build_ready_task_with_knowledge(
        self, session, storage: LocalArtifactStorage
    ) -> tuple[Task, Artifact, Artifact]:
        task = self._build_ready_task(session)
        goal = session.get(Goal, task.goal_id)
        project = session.get(Project, goal.project_id)

        source = Artifact(
            project_id=project.id,
            goal_id=goal.id,
            name="knowledge.md",
            kind="source",
            content_type="text/markdown",
            ingestion_status="pending",
        )
        session.add(source)
        session.flush()
        create_artifact_version(session, storage, source, b"# Title\nBody\n", version_number=1)
        normalized = ingest_source_artifact(session, storage, source)
        self.assertIsNotNone(normalized)

        task.knowledge_artifact_ids = [str(source.id)]
        session.add(task)
        session.flush()
        session.commit()
        return task, source, normalized

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
            self.assertEqual(run.snapshot["agent_version_id"], str(run.agent_version_id))
            configuration_snapshot = session.get(
                RunConfigurationSnapshot, uuid.UUID(run.snapshot["configuration_snapshot_id"])
            )
            self.assertIsNotNone(configuration_snapshot)
            self.assertEqual(configuration_snapshot.run_id, run.id)
            self.assertEqual(
                configuration_snapshot.configuration["snapshot_id"],
                run.snapshot["configuration_snapshot_id"],
            )
            self.assertEqual(
                run.snapshot["assignment_rationale"]["selected_agent_version_id"],
                str(run.agent_version_id),
            )
            self.assertEqual(
                run.snapshot["capability_manifest"]["skill_version_id"],
                run.snapshot["skill_version_id"],
            )

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
            reservation = session.execute(
                select(BudgetReservation).where(BudgetReservation.run_id == run.id)
            ).scalar_one()
            self.assertEqual(reservation.status, "reconciled")
            self.assertEqual(ledger_entries[0].reservation_id, reservation.id)

            artifact_versions = list(
                session.execute(
                    select(ArtifactVersion)
                    .join(Artifact, ArtifactVersion.artifact_id == Artifact.id)
                    .where(Artifact.run_id == run.id)
                ).scalars()
            )
            self.assertEqual(len(artifact_versions), 1)

            evidence_events = list(
                session.execute(
                    select(AuditEvent).where(
                        AuditEvent.run_id == run.id,
                        AuditEvent.event_type.in_(["run.started", "policy.decision", "tool.invoked"]),
                    )
                ).scalars()
            )
            self.assertTrue(evidence_events)
            self.assertTrue(
                all(
                    event.payload["configuration_snapshot_id"]
                    == run.snapshot["configuration_snapshot_id"]
                    for event in evidence_events
                )
            )

        # Re-running the worker must not duplicate the already-completed task.
        with self.Session() as session:
            second_claim = run_task_worker_once(session, "worker-b")
            session.commit()
            self.assertIsNone(second_claim)

        with self.Session() as session:
            runs = list(session.execute(select(Run).where(Run.task_id == task_id)).scalars())
            self.assertEqual(len(runs), 1)

    def test_worker_cannot_complete_when_finalized_artifact_content_disappears(self) -> None:
        class DisappearingStorage(LocalArtifactStorage):
            def finalize(self, staged):
                storage_ref = super().finalize(staged)
                self.path_for_ref(storage_ref).unlink()
                return storage_ref

        with self.Session() as session:
            task = self._build_ready_task(session)
            task_id = task.id

        with tempfile.TemporaryDirectory() as directory:
            storage = DisappearingStorage(directory)
            with self.Session() as session:
                with mock.patch(
                    "agentic_os.worker.runner.artifact_storage", return_value=storage
                ):
                    with self.assertRaises(ArtifactContentUnavailableError):
                        run_task_worker_once(session, "worker-missing-artifact")
                session.commit()

        with self.Session() as session:
            task = session.get(Task, task_id)
            run = session.execute(select(Run).where(Run.task_id == task_id)).scalar_one()
            versions = list(
                session.execute(
                    select(ArtifactVersion)
                    .join(Artifact, ArtifactVersion.artifact_id == Artifact.id)
                    .where(Artifact.run_id == run.id)
                ).scalars()
            )
            self.assertEqual(task.status, "failed")
            self.assertEqual(run.status, "failed")
            self.assertEqual(versions, [])

    def test_unconfigured_tool_access_fails_before_invocation_with_audit_state(self) -> None:
        with self.Session() as session:
            task = self._build_ready_task(session, tools=("not-attached",))
            task_id = task.id

        with self.Session() as session:
            with mock.patch("agentic_os.worker.runner.invoke_tool") as invoke:
                with self.assertRaisesRegex(TaskExecutionError, "unconfigured tools"):
                    run_task_worker_once(session, "worker-unconfigured")
                invoke.assert_not_called()
            session.commit()

        with self.Session() as session:
            task = session.get(Task, task_id)
            run = session.execute(select(Run).where(Run.task_id == task_id)).scalar_one()
            self.assertEqual(task.status, "failed")
            self.assertEqual(run.status, "failed")
            self.assertIn("assigned_agent_version_id", run.snapshot)
            event_types = {
                event.event_type
                for event in session.execute(
                    select(AuditEvent).where(AuditEvent.task_id == task_id)
                ).scalars()
            }
            self.assertIn("task.failed", event_types)
            self.assertNotIn("tool.invoked", event_types)

    def test_retry_reuses_snapshot_after_configuration_rows_change(self) -> None:
        with self.Session() as session:
            task = self._build_ready_task(session)
            task_id = task.id

        with self.Session() as session:
            with mock.patch(
                "agentic_os.worker.runner.invoke_tool", side_effect=RuntimeError("simulated crash")
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    run_task_worker_once(session, "worker-first")
            session.commit()

        with self.Session() as session:
            task = session.get(Task, task_id)
            first_run = session.execute(select(Run).where(Run.task_id == task_id)).scalar_one()
            snapshot_id = first_run.snapshot["configuration_snapshot_id"]
            agent_version = session.get(AgentVersion, task.assigned_agent_version_id)
            agent_version.capability_manifest = {"enabled_tools": []}
            budget = session.get(Budget, uuid.UUID(first_run.snapshot["default_budget_id"]))
            budget.amount_minor_units = 0
            session.add(
                Policy(scope_type="agent", scope_id=agent_version.agent_id, decision="deny", rule={})
            )
            mcp_attachment = session.execute(
                select(AgentVersionMcpServer).where(
                    AgentVersionMcpServer.agent_version_id == agent_version.id
                )
            ).scalar_one()
            mcp_version = session.get(McpServerVersion, mcp_attachment.mcp_server_version_id)
            mcp_version.connection_config = {"tools": []}
            skill_attachment = session.execute(
                select(AgentVersionSkill).where(AgentVersionSkill.agent_version_id == agent_version.id)
            ).scalar_one()
            skill_version = session.get(SkillVersion, skill_attachment.skill_version_id)
            skill_version.content_ref = "skills/changed-after-snapshot/v2"
            task.status = "pending"
            session.commit()

        with self.Session() as session:
            claimed = run_task_worker_once(session, "worker-retry")
            session.commit()
            self.assertEqual(claimed.status, "completed")

        with self.Session() as session:
            runs = list(
                session.execute(
                    select(Run).where(Run.task_id == task_id).order_by(Run.attempt_number)
                ).scalars()
            )
            self.assertEqual([run.status for run in runs], ["failed", "completed"])
            self.assertEqual(runs[0].snapshot["configuration_snapshot_id"], snapshot_id)
            self.assertEqual(runs[1].snapshot["configuration_snapshot_id"], snapshot_id)
            self.assertEqual(runs[1].snapshot["enabled_tools"], ["echo"])
            self.assertEqual(runs[1].snapshot["policy_decision"], "allow")
            snapshots = list(
                session.execute(
                    select(RunConfigurationSnapshot)
                    .join(Run, RunConfigurationSnapshot.run_id == Run.id)
                    .where(Run.task_id == task_id)
                ).scalars()
            )
            self.assertEqual(len(snapshots), 1)
            retry_skill_event = session.execute(
                select(AuditEvent).where(
                    AuditEvent.run_id == runs[1].id, AuditEvent.event_type == "skill.invoked"
                )
            ).scalar_one()
            self.assertEqual(retry_skill_event.payload["content_ref"], "skills/worker/v1")

    def test_worker_consumes_project_knowledge_and_publishes_cited_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalArtifactStorage(directory)
            with self.Session() as session:
                task, source, normalized = self._build_ready_task_with_knowledge(session, storage)
                task_id = task.id
                source_id = source.id
                normalized_id = normalized.id

            with self.Session() as session:
                with mock.patch("agentic_os.worker.runner.artifact_storage", return_value=storage):
                    claimed = run_task_worker_once(session, "worker-a")
                session.commit()
                self.assertIsNotNone(claimed)
                self.assertEqual(claimed.status, "completed")

            with self.Session() as session:
                run = session.execute(select(Run).where(Run.task_id == task_id)).scalar_one()
                output_artifact = session.execute(
                    select(Artifact).where(Artifact.task_id == task_id, Artifact.kind == "output")
                ).scalar_one()

                citations = list(
                    session.execute(
                        select(ArtifactCitation).where(ArtifactCitation.output_artifact_id == output_artifact.id)
                    ).scalars()
                )
                self.assertEqual(len(citations), 1)
                self.assertEqual(citations[0].run_id, run.id)
                self.assertEqual(citations[0].task_id, task_id)
                self.assertEqual(citations[0].source_artifact_id, source_id)
                self.assertEqual(citations[0].normalized_artifact_id, normalized_id)
                self.assertIn("source_byte_span", citations[0].citation_anchor)

                event_types = [
                    row.event_type
                    for row in session.execute(
                        select(AuditEvent).where(AuditEvent.run_id == run.id).order_by(AuditEvent.sequence_number)
                    ).scalars()
                ]
                self.assertEqual(
                    event_types,
                    [
                        "run.started",
                        "policy.decision",
                        "artifact.knowledge_consumed",
                        "tool.invoked",
                        "skill.invoked",
                        "artifact.citations_recorded",
                        "artifact.output_published",
                        "run.completed",
                    ],
                )

                output_version = session.execute(
                    select(ArtifactVersion).where(ArtifactVersion.artifact_id == output_artifact.id)
                ).scalar_one()
                payload = json.loads(storage.read(output_version.storage_ref))
                self.assertEqual(len(payload["citations"]), 1)
                self.assertEqual(payload["citations"][0]["source_artifact_id"], str(source_id))
                self.assertEqual(payload["citations"][0]["normalized_artifact_id"], str(normalized_id))

    def test_worker_fails_safely_when_knowledge_artifact_is_missing(self) -> None:
        missing_artifact_id = uuid.uuid4()
        with self.Session() as session:
            task = self._build_ready_task(session)
            task.knowledge_artifact_ids = [str(missing_artifact_id)]
            session.add(task)
            session.commit()
            task_id = task.id

        with self.Session() as session:
            with self.assertRaises(KnowledgeUnavailableError):
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
            self.assertIn("artifact.knowledge_unavailable", event_types)
            self.assertIn("task.failed", event_types)
            self.assertNotIn("tool.invoked", event_types)

            output_artifacts = list(
                session.execute(
                    select(Artifact).where(Artifact.task_id == task_id, Artifact.kind == "output")
                ).scalars()
            )
            self.assertEqual(output_artifacts, [])

            citations = list(
                session.execute(select(ArtifactCitation).where(ArtifactCitation.task_id == task_id)).scalars()
            )
            self.assertEqual(citations, [])

    def test_knowledge_citations_persist_across_interrupted_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalArtifactStorage(directory)
            with self.Session() as session:
                task, source, _normalized = self._build_ready_task_with_knowledge(session, storage)
                task_id = task.id
                source_id = source.id

            # worker-crashed claims the task but dies before a run is ever
            # created; its lease is never renewed or released.
            with self.Session() as session:
                claim_ready_task(session, "worker-crashed", lease_seconds=60)
                session.commit()

            with self.Session() as session:
                task = session.get(Task, task_id)
                task.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                session.commit()

            with self.Session() as session:
                with mock.patch("agentic_os.worker.runner.artifact_storage", return_value=storage):
                    claimed = run_task_worker_once(session, "worker-recovering")
                session.commit()
                self.assertIsNotNone(claimed)
                self.assertEqual(claimed.status, "completed")

            with self.Session() as session:
                runs = list(
                    session.execute(
                        select(Run).where(Run.task_id == task_id).order_by(Run.attempt_number)
                    ).scalars()
                )
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0].status, "completed")
                self.assertEqual(runs[0].attempt_number, 1)

                citations = list(
                    session.execute(select(ArtifactCitation).where(ArtifactCitation.task_id == task_id)).scalars()
                )
                self.assertEqual(len(citations), 1)
                self.assertEqual(citations[0].run_id, runs[0].id)
                self.assertEqual(citations[0].source_artifact_id, source_id)

                knowledge_events = [
                    row.event_type
                    for row in session.execute(
                        select(AuditEvent).where(AuditEvent.task_id == task_id).order_by(AuditEvent.sequence_number)
                    ).scalars()
                ]
                self.assertEqual(knowledge_events.count("artifact.knowledge_consumed"), 1)
                self.assertEqual(knowledge_events.count("artifact.citations_recorded"), 1)

    def test_worker_executes_task_with_sandbox_and_persists_lifecycle_events(self) -> None:
        available, reason = runtime_available("docker")
        if not available:
            available, reason = runtime_available("podman")
            if not available:
                self.skipTest(f"no sandbox runtime available: {reason}")

        sandbox_config = {
            "image": "alpine:latest",
            "command": ["true"],
            "network_policy": "none",
            "cpu_limit": 1.0,
            "memory_limit_mb": 256,
            "timeout_seconds": 30,
        }

        with self.Session() as session:
            task = self._build_ready_task(session, sandbox=sandbox_config)
            task_id = task.id

        with self.Session() as session:
            claimed = run_task_worker_once(session, "worker-sandbox")
            session.commit()
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.status, "completed")

        with self.Session() as session:
            run = session.execute(select(Run).where(Run.task_id == task_id)).scalar_one()
            self.assertEqual(run.status, "completed")

            event_types = [
                row.event_type
                for row in session.execute(
                    select(AuditEvent).where(AuditEvent.run_id == run.id).order_by(AuditEvent.sequence_number)
                ).scalars()
            ]
            self.assertEqual(
                event_types,
                [
                    "run.started",
                    "policy.decision",
                    "tool.invoked",
                    "skill.invoked",
                    "sandbox.created",
                    "sandbox.started",
                    "sandbox.exited",
                    "sandbox.stopped",
                    "sandbox.cleaned_up",
                    "run.completed",
                ],
            )

            sandbox_events = list(
                session.execute(
                    select(AuditEvent).where(
                        AuditEvent.run_id == run.id, AuditEvent.event_type.like("sandbox.%")
                    )
                ).scalars()
            )
            for event in sandbox_events:
                self.assertEqual(event.task_id, task_id)
                self.assertEqual(event.goal_id, task.goal_id)
                self.assertIsNotNone(event.project_id)
            exited_event = next(e for e in sandbox_events if e.event_type == "sandbox.exited")
            self.assertEqual(exited_event.payload["exit_code"], 0)
            self.assertFalse(exited_event.payload["timed_out"])

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

    def test_mcp_server_policy_deny_blocks_execution_before_tool_invocation(self) -> None:
        with self.Session() as session:
            task = self._build_ready_task(session)
            task_id = task.id
            agent_version = session.get(AgentVersion, task.assigned_agent_version_id)
            mcp_server_version_id = uuid.UUID(agent_version.capability_manifest["mcp_server_version_id"])
            mcp_server_version = session.get(McpServerVersion, mcp_server_version_id)
            session.add(
                Policy(scope_type="mcp_server", scope_id=mcp_server_version.mcp_server_id, decision="deny", rule={})
            )
            session.commit()

        with self.Session() as session:
            with self.assertRaises(TaskExecutionError):
                run_task_worker_once(session, "worker-a")
            session.commit()

        with self.Session() as session:
            task = session.get(Task, task_id)
            self.assertEqual(task.status, "failed")

            run = session.execute(select(Run).where(Run.task_id == task_id)).scalar_one()
            self.assertEqual(run.status, "failed")
            self.assertEqual(run.snapshot["policy_decision"], "deny")

            event_types = {
                row.event_type
                for row in session.execute(select(AuditEvent).where(AuditEvent.task_id == task_id)).scalars()
            }
            self.assertIn("policy.decision", event_types)
            self.assertNotIn("tool.invoked", event_types)

    def test_policy_approval_required_blocks_task_in_safe_state(self) -> None:
        with self.Session() as session:
            task = self._build_ready_task(session)
            task_id = task.id
            agent_version = session.get(AgentVersion, task.assigned_agent_version_id)
            session.add(
                Policy(
                    scope_type="agent",
                    scope_id=agent_version.agent_id,
                    decision="approval_required",
                    rule={},
                )
            )
            session.commit()

        with self.Session() as session:
            claimed = run_task_worker_once(session, "worker-a")
            session.commit()
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.status, "blocked")
            self.assertIsNone(claimed.lease_owner)

        with self.Session() as session:
            task = session.get(Task, task_id)
            self.assertEqual(task.status, "blocked")
            self.assertIsNone(task.lease_owner)

            run = session.execute(select(Run).where(Run.task_id == task_id)).scalar_one()
            self.assertEqual(run.status, "waiting_approval")
            self.assertIsNone(run.completed_at)
            self.assertEqual(run.snapshot["policy_decision"], "approval_required")
            approval = session.execute(
                select(ApprovalRequest).where(ApprovalRequest.task_id == task_id)
            ).scalar_one()
            approval_id = approval.id
            self.assertEqual(approval.action_type, "run.execution")
            self.assertEqual(run.snapshot["approval_request_ids"], [str(approval.id)])

            event_types = {
                row.event_type
                for row in session.execute(select(AuditEvent).where(AuditEvent.task_id == task_id)).scalars()
            }
            self.assertIn("policy.approval_required", event_types)
            self.assertNotIn("tool.invoked", event_types)
            self.assertNotIn("task.failed", event_types)

        # An approval made in another process/session returns the task to the
        # claim queue. The retry reuses the original request and immutable
        # configuration snapshot, then dispatches the tool exactly once.
        with self.Session() as session:
            approval = session.get(ApprovalRequest, approval_id)
            approval.status = "approved"
            approval.resolved_at = datetime.now(timezone.utc)
            task = session.get(Task, task_id)
            task.status = "ready"
            session.commit()

        with self.Session() as session:
            with mock.patch(
                "agentic_os.worker.runner.invoke_tool",
                side_effect=lambda _name, arguments: {"echo": arguments},
            ) as invoke:
                resumed = run_task_worker_once(session, "worker-b")
                session.commit()
                self.assertEqual(resumed.status, "completed")
                invoke.assert_called_once()

        with self.Session() as session:
            requests = list(
                session.execute(select(ApprovalRequest).where(ApprovalRequest.task_id == task_id)).scalars()
            )
            runs = list(
                session.execute(
                    select(Run).where(Run.task_id == task_id).order_by(Run.attempt_number)
                ).scalars()
            )
            self.assertEqual([request.id for request in requests], [approval_id])
            self.assertEqual([run.status for run in runs], ["waiting_approval", "completed"])
            self.assertEqual(
                runs[0].snapshot["configuration_snapshot_id"],
                runs[1].snapshot["configuration_snapshot_id"],
            )

    def test_consequential_mode_materializes_all_required_requests_before_side_effects(self) -> None:
        with self.Session() as session:
            task = self._build_ready_task(
                session,
                sandbox={
                    "image": "alpine:latest",
                    "command": ["true"],
                    "network_policy": "restricted",
                },
            )
            task.resource_intent = [{"resource_key": "docs/report.md", "intent": "write"}]
            goal = session.get(Goal, task.goal_id)
            project = session.get(Project, goal.project_id)
            session.add(
                ApprovalModeConfiguration(
                    team_id=project.team_id,
                    project_id=project.id,
                    goal_id=goal.id,
                    configured_by=goal.created_by,
                    version_number=1,
                    mode="consequential",
                    consequential_action_types=[
                        "skill.access",
                        "mcp.call",
                        "sandbox.lifecycle",
                        "network.permission",
                        "resource.permission",
                        "workspace.promotion",
                        "artifact.promotion",
                    ],
                )
            )
            task_id = task.id
            session.commit()

        with self.Session() as session:
            with mock.patch("agentic_os.worker.runner.invoke_tool") as invoke, mock.patch(
                "agentic_os.worker.runner.execute_task_sandbox"
            ) as sandbox:
                task = run_task_worker_once(session, "worker-approval")
                session.commit()
                self.assertEqual(task.status, "blocked")
                invoke.assert_not_called()
                sandbox.assert_not_called()

        with self.Session() as session:
            requests = list(
                session.execute(
                    select(ApprovalRequest)
                    .where(ApprovalRequest.task_id == task_id)
                    .order_by(ApprovalRequest.action_type)
                ).scalars()
            )
            self.assertEqual(
                {request.action_type for request in requests},
                {
                    "artifact.promotion",
                    "mcp.call",
                    "network.permission",
                    "resource.permission",
                    "sandbox.lifecycle",
                    "skill.access",
                    "workspace.promotion",
                },
            )
            self.assertTrue(all(request.status == "pending" for request in requests))

    def test_every_tool_call_mode_gates_each_tool_before_dispatch(self) -> None:
        with self.Session() as session:
            task = self._build_ready_task(session)
            goal = session.get(Goal, task.goal_id)
            project = session.get(Project, goal.project_id)
            session.add(
                ApprovalModeConfiguration(
                    team_id=project.team_id,
                    project_id=project.id,
                    configured_by=goal.created_by,
                    version_number=1,
                    mode="every_tool_call",
                )
            )
            task_id = task.id
            session.commit()

        with self.Session() as session:
            with mock.patch("agentic_os.worker.runner.invoke_tool") as invoke:
                task = run_task_worker_once(session, "worker-every-tool")
                session.commit()
                self.assertEqual(task.status, "blocked")
                invoke.assert_not_called()

        with self.Session() as session:
            requests = list(
                session.execute(select(ApprovalRequest).where(ApprovalRequest.task_id == task_id)).scalars()
            )
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].action_type, "mcp.call")
            self.assertEqual(requests[0].action_preview["tool"], "echo")

    def test_budget_hard_stop_blocks_chargeable_tool_before_invocation(self) -> None:
        with self.Session() as session:
            task = self._build_ready_task(
                session,
                tool_pricing={"chargeable": True, "amount_minor_units": 5_00, "currency": "USD"},
                budget_amount_minor_units=3_00,
            )
            task_id = task.id

        with self.Session() as session:
            with self.assertRaises(TaskExecutionError):
                run_task_worker_once(session, "worker-a")
            session.commit()

        with self.Session() as session:
            task = session.get(Task, task_id)
            self.assertEqual(task.status, "failed")

            run = session.execute(select(Run).where(Run.task_id == task_id)).scalar_one()
            self.assertEqual(run.status, "failed")
            self.assertEqual(run.snapshot["policy_decision"], "allow")

            event_types = {
                row.event_type
                for row in session.execute(select(AuditEvent).where(AuditEvent.task_id == task_id)).scalars()
            }
            self.assertIn("budget.exhausted", event_types)
            self.assertIn("task.failed", event_types)
            self.assertNotIn("tool.invoked", event_types)

            ledger_entries = list(
                session.execute(select(CostLedgerEntry).where(CostLedgerEntry.run_id == run.id)).scalars()
            )
            self.assertEqual(len(ledger_entries), 1)
            self.assertEqual(ledger_entries[0].status, "void")
            self.assertEqual(ledger_entries[0].reserved_amount_minor_units, 500)
            self.assertIsNone(ledger_entries[0].actual_amount_minor_units)
            self.assertFalse(ledger_entries[0].is_zero_cost)

    def test_budget_warning_mode_allows_chargeable_tool_past_threshold(self) -> None:
        with self.Session() as session:
            task = self._build_ready_task(
                session,
                tool_pricing={"chargeable": True, "amount_minor_units": 5_00, "currency": "USD"},
                budget_amount_minor_units=3_00,
                budget_enforcement_mode="warning",
                budget_warning_threshold_percent=80,
            )
            task_id = task.id

        with self.Session() as session:
            claimed = run_task_worker_once(session, "worker-a")
            session.commit()
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.status, "completed")

        with self.Session() as session:
            run = session.execute(select(Run).where(Run.task_id == task_id)).scalar_one()
            self.assertEqual(run.status, "completed")

            ledger_entries = list(
                session.execute(select(CostLedgerEntry).where(CostLedgerEntry.run_id == run.id)).scalars()
            )
            self.assertEqual(len(ledger_entries), 1)
            self.assertEqual(ledger_entries[0].status, "reconciled")
            self.assertEqual(ledger_entries[0].reserved_amount_minor_units, 500)
            self.assertEqual(ledger_entries[0].actual_amount_minor_units, 500)
            self.assertFalse(ledger_entries[0].is_zero_cost)
            self.assertTrue(ledger_entries[0].warning_triggered)

            reservation = session.execute(
                select(BudgetReservation).where(BudgetReservation.run_id == run.id)
            ).scalar_one()
            self.assertEqual(reservation.status, "reconciled")
            self.assertTrue(reservation.warning_triggered)

            event_types = {
                row.event_type
                for row in session.execute(select(AuditEvent).where(AuditEvent.task_id == task_id)).scalars()
            }
            self.assertIn("tool.invoked", event_types)
            self.assertIn("budget.warning_threshold", event_types)

    def test_unpriced_metered_tool_is_rejected_before_hard_budget_side_effect(self) -> None:
        with self.Session() as session:
            task = self._build_ready_task(
                session,
                tool_pricing={"chargeable": True, "currency": "USD"},
            )
            task_id = task.id

        with self.Session() as session:
            with mock.patch("agentic_os.worker.runner.invoke_tool") as invoke:
                with self.assertRaises(TaskExecutionError):
                    run_task_worker_once(session, "worker-unpriced")
                session.commit()
                invoke.assert_not_called()

        with self.Session() as session:
            run = session.execute(select(Run).where(Run.task_id == task_id)).scalar_one()
            reservation = session.execute(
                select(BudgetReservation).where(BudgetReservation.run_id == run.id)
            ).scalar_one()
            ledger = session.execute(
                select(CostLedgerEntry).where(CostLedgerEntry.run_id == run.id)
            ).scalar_one()
            self.assertEqual(reservation.status, "rejected")
            self.assertTrue(reservation.is_unpriced)
            self.assertTrue(reservation.hard_stop_triggered)
            self.assertEqual(ledger.status, "void")
            self.assertTrue(ledger.is_unpriced)

    def test_active_admin_override_allows_scoped_over_budget_action(self) -> None:
        with self.Session() as session:
            task = self._build_ready_task(
                session,
                tool_pricing={"chargeable": True, "amount_minor_units": 500, "currency": "USD"},
                budget_amount_minor_units=300,
            )
            goal = session.get(Goal, task.goal_id)
            project = session.get(Project, goal.project_id)
            admin = User(
                email=f"admin-{uuid.uuid4()}@example.test",
                display_name="Budget Admin",
                role="admin",
            )
            session.add(admin)
            session.flush()
            override = AdminOverride(
                team_id=project.team_id,
                project_id=project.id,
                goal_id=goal.id,
                task_id=task.id,
                created_by=admin.id,
                scope_type="task",
                scope_id=task.id,
                reason="Allow one bounded over-limit tool call",
                starts_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                context={"budget": {"allow_over_limit": True}},
            )
            session.add(override)
            session.flush()
            task_id = task.id
            override_id = override.id
            admin_id = admin.id
            session.commit()

        with self.Session() as session:
            claimed = run_task_worker_once(session, "worker-override")
            session.commit()
            self.assertEqual(claimed.status, "completed")

        with self.Session() as session:
            run = session.execute(select(Run).where(Run.task_id == task_id)).scalar_one()
            reservation = session.execute(
                select(BudgetReservation).where(BudgetReservation.run_id == run.id)
            ).scalar_one()
            ledger = session.execute(
                select(CostLedgerEntry).where(CostLedgerEntry.run_id == run.id)
            ).scalar_one()
            self.assertEqual(reservation.status, "reconciled")
            self.assertTrue(reservation.hard_stop_triggered)
            self.assertEqual(reservation.pricing_evidence["override"]["id"], str(override_id))
            self.assertEqual(ledger.evidence["override"]["actor_id"], str(admin_id))
            events = {
                row.event_type
                for row in session.execute(select(AuditEvent).where(AuditEvent.run_id == run.id)).scalars()
            }
            self.assertIn("budget.override_applied", events)

    def test_timed_out_tool_keeps_reservation_for_reconciliation(self) -> None:
        with self.Session() as session:
            task = self._build_ready_task(
                session,
                tool_pricing={"chargeable": True, "amount_minor_units": 200, "currency": "USD"},
            )
            task_id = task.id

        with self.Session() as session:
            with mock.patch(
                "agentic_os.worker.runner.invoke_tool", side_effect=TimeoutError("provider timeout")
            ):
                with self.assertRaises(TaskExecutionError):
                    run_task_worker_once(session, "worker-timeout")
                session.commit()

        with self.Session() as session:
            run = session.execute(select(Run).where(Run.task_id == task_id)).scalar_one()
            reservation = session.execute(
                select(BudgetReservation).where(BudgetReservation.run_id == run.id)
            ).scalar_one()
            ledger = session.execute(
                select(CostLedgerEntry).where(CostLedgerEntry.run_id == run.id)
            ).scalar_one()
            self.assertEqual(reservation.status, "active")
            self.assertEqual(ledger.status, "reserved")
            self.assertEqual(ledger.evidence["outcome"], "uncertain_external_side_effect")
            events = {
                row.event_type
                for row in session.execute(select(AuditEvent).where(AuditEvent.run_id == run.id)).scalars()
            }
            self.assertIn("budget.reconciliation_required", events)

    def test_concurrent_hard_budget_reservations_cannot_overspend(self) -> None:
        with self.Session() as session:
            task = self._build_ready_task(session, budget_amount_minor_units=1_000)
            goal = session.get(Goal, task.goal_id)
            project = session.get(Project, goal.project_id)
            version = session.get(AgentVersion, task.assigned_agent_version_id)
            budget = session.get(Budget, version.default_budget_id)
            run = Run(
                task_id=task.id,
                attempt_number=99,
                idempotency_key=f"{task.id}:concurrent-budget-test",
                lease_token=task.lease_token,
                agent_version_id=version.id,
                status="running",
            )
            session.add(run)
            task.status = "completed"
            session.flush()
            context = BudgetActionContext(
                team_id=project.team_id,
                project_id=project.id,
                goal_id=goal.id,
                task_id=task.id,
                run_id=run.id,
                agent_version_id=version.id,
                requested_by=goal.created_by,
            )
            limit = BudgetLimit(
                id=budget.id,
                currency=budget.currency,
                amount_minor_units=budget.amount_minor_units,
                enforcement_mode=budget.enforcement_mode,
            )
            session.commit()

        barrier = threading.Barrier(2)

        def claim() -> str:
            with self.Session() as session:
                barrier.wait()
                try:
                    reserve_action_cost(
                        session,
                        budget=limit,
                        context=context,
                        action_type="model_call",
                        amount_minor_units=600,
                        currency="USD",
                    )
                    session.commit()
                    return "reserved"
                except BudgetExhaustedError:
                    session.commit()
                    return "rejected"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: claim(), range(2)))
        self.assertCountEqual(results, ["reserved", "rejected"])

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
