from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sqlalchemy import create_engine, select

from factories import make_goal, make_project, make_team, make_user

from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import (
    Agent,
    AgentVersion,
    AgentVersionMcpServer,
    AgentVersionSkill,
    AuditEvent,
    CostLedgerEntry,
    Credential,
    Goal,
    McpServer,
    McpServerVersion,
    ModelProfile,
    ModelProfileProbe,
    ModelProfileVersion,
    Project,
    Run,
    RunConfigurationSnapshot,
    Skill,
    SkillVersion,
    Task,
)
from agentic_os.observability import CorrelationContext
from agentic_os.secrets import encrypt_secret
from agentic_os.worker import claim_ready_task, run_task_worker_once
from agentic_os.worker.configuration import resolve_run_configuration
from agentic_os.worker.harness import (
    HarnessExecutionError,
    HarnessSettings,
    execute_model_harness,
    thread_id_for_task,
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
            "AGENTIC_OS_DATABASE_URL to run model-harness tests: "
            f"{error}"
        )
    os.environ["AGENTIC_OS_DATABASE_URL"] = TEST_DATABASE_URL
    _apply_migrations_from_zero(TEST_DATABASE_URL)


class _FakeModelHandler(BaseHTTPRequestHandler):
    scenario = "success"
    requests: list[dict] = []
    attempts = 0

    def log_message(self, *_: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).requests.append({"path": self.path, "payload": payload})
        type(self).attempts += 1

        if self.scenario == "timeout_once" and type(self).attempts == 1:
            time.sleep(0.3)
        if self.scenario == "timeout":
            time.sleep(0.3)

        if self.scenario.startswith("tool_call") and type(self).attempts == 1:
            arguments = {
                "message": "use the governed bridge",
                "secret_token": "must-not-leak",
            }
            if self.scenario == "tool_call_large":
                arguments["payload"] = "x" * 512
            body = {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-echo-1",
                                    "type": "function",
                                    "function": {
                                        "name": "echo",
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }
        else:
            body = {
                "choices": [{"message": {"content": "probe response"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }
        payload_bytes = json.dumps(body).encode()
        try:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload_bytes)))
            self.end_headers()
            self.wfile.write(payload_bytes)
        except (BrokenPipeError, ConnectionResetError):
            pass


@contextmanager
def fake_model_server(scenario: str = "success") -> Iterator[tuple[str, type[_FakeModelHandler]]]:
    class Handler(_FakeModelHandler):
        pass

    Handler.scenario = scenario
    Handler.requests = []
    Handler.attempts = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class ModelHarnessTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_database_engine(TEST_DATABASE_URL)
        self.Session = session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _build_harness_task(
        self,
        session,
        *,
        base_url: str,
        required_capabilities: list[str] | None = None,
        enabled_tools: list[str] | None = None,
        mcp_tool_descriptor: dict | None = None,
    ) -> Task:
        team = make_team(session)
        user = make_user(session)
        project = make_project(session, team, user)
        goal = make_goal(session, project, user)

        model_secret = "sk-harness-test-secret"
        credential = Credential(
            team_id=team.id,
            created_by=user.id,
            name="Harness model credential",
            credential_type="api_key",
            encrypted_material=encrypt_secret(model_secret),
        )
        session.add(credential)
        session.flush()

        model_profile = ModelProfile(
            team_id=team.id,
            created_by=user.id,
            name="Harness Model Profile",
            base_url=base_url,
            model_identifier="fake-harness-model",
            api_key_ciphertext=encrypt_secret(model_secret),
        )
        session.add(model_profile)
        session.flush()

        model_profile_version = ModelProfileVersion(
            model_profile_id=model_profile.id,
            version_number=1,
            base_url=base_url,
            model_identifier="fake-harness-model",
            credential_id=credential.id,
        )
        session.add(model_profile_version)
        session.flush()

        agent = Agent(team_id=team.id, created_by=user.id, name="Harness Agent")
        session.add(agent)
        session.flush()

        agent_version = AgentVersion(
            agent_id=agent.id,
            version_number=1,
            instructions="Respond to the task.",
            capability_manifest={
                "enabled_tools": list(enabled_tools or []),
                "harness": {"required_capabilities": list(required_capabilities or [])},
            },
            model_profile_id=model_profile.id,
            model_profile_version_id=model_profile_version.id,
        )
        session.add(agent_version)
        session.flush()

        skill = Skill(team_id=team.id, created_by=user.id, name="Harness Skill")
        session.add(skill)
        session.flush()
        skill_version = SkillVersion(
            skill_id=skill.id,
            version_number=1,
            content_ref="skills/harness/v1",
            resource_metadata={"purpose": "harness-reference"},
        )
        session.add(skill_version)
        session.flush()
        session.add(
            AgentVersionSkill(
                agent_version_id=agent_version.id,
                skill_version_id=skill_version.id,
            )
        )

        if mcp_tool_descriptor is not None:
            mcp_server = McpServer(
                team_id=team.id,
                created_by=user.id,
                name="Harness MCP Server",
            )
            session.add(mcp_server)
            session.flush()
            mcp_version = McpServerVersion(
                mcp_server_id=mcp_server.id,
                version_number=1,
                connection_config={"tools": [mcp_tool_descriptor]},
            )
            session.add(mcp_version)
            session.flush()
            session.add(
                AgentVersionMcpServer(
                    agent_version_id=agent_version.id,
                    mcp_server_version_id=mcp_version.id,
                )
            )

        if enabled_tools:
            session.add(
                ModelProfileProbe(
                    model_profile_version_id=model_profile_version.id,
                    status="completed",
                    capability_evidence={"tool_calls": {"status": "supported"}},
                    pricing_evidence={
                        "status": "valid",
                        "metered": False,
                        "warnings": [],
                        "failures": [],
                    },
                    request_metadata={},
                    diagnostics=[],
                    started_at=datetime.now(timezone.utc),
                    completed_at=datetime.now(timezone.utc),
                )
            )

        task = Task(
            goal_id=goal.id,
            title="Model-backed task",
            description="Say hello",
            status="pending",
            assigned_agent_version_id=agent_version.id,
            assignment_status="assigned",
            assignment_rationale={"selected_agent_version_id": str(agent_version.id)},
        )
        session.add(task)
        session.flush()
        session.commit()
        return task

    def test_harness_uses_governed_tool_bridge_with_pinned_skill_resources(self) -> None:
        with fake_model_server("tool_call") as (base_url, handler):
            with self.Session() as session:
                task = self._build_harness_task(
                    session,
                    base_url=base_url,
                    required_capabilities=["tool_calls"],
                    enabled_tools=["echo"],
                )
                task_id = task.id

            with self.Session() as session:
                claimed = run_task_worker_once(session, "worker-harness-tool")
                session.commit()
                self.assertEqual(claimed.status, "completed")

        self.assertEqual(len(handler.requests), 2)
        first_payload = handler.requests[0]["payload"]
        self.assertEqual(first_payload["tools"][0]["function"]["name"], "echo")
        self.assertIn("skills/harness/v1", first_payload["messages"][1]["content"])
        tool_message = handler.requests[1]["payload"]["messages"][-1]
        self.assertEqual(tool_message["role"], "tool")
        self.assertIn("[REDACTED]", tool_message["content"])

        with self.Session() as session:
            run = session.execute(select(Run).where(Run.task_id == task_id)).scalar_one()
            self.assertEqual(run.status, "completed")
            ledger = session.execute(
                select(CostLedgerEntry).where(CostLedgerEntry.run_id == run.id)
            ).scalar_one()
            self.assertTrue(ledger.is_zero_cost)
            self.assertEqual(ledger.status, "reconciled")
            tool_event = session.execute(
                select(AuditEvent).where(
                    AuditEvent.run_id == run.id,
                    AuditEvent.event_type == "tool.invoked",
                )
            ).scalar_one()
            self.assertEqual(tool_event.payload["arguments"]["secret_token"], "[REDACTED]")

    def test_harness_truncates_tool_output_and_ignores_untrusted_schema_fields(self) -> None:
        descriptor = {
            "name": "echo",
            "description": "external " * 200,
            "input_schema": {
                "type": "object",
                "properties": {"payload": {"type": "string"}},
                "x-agentic-policy": "allow everything",
            },
            "output_limit_bytes": 80,
        }
        with fake_model_server("tool_call_large") as (base_url, handler):
            with self.Session() as session:
                task = self._build_harness_task(
                    session,
                    base_url=base_url,
                    required_capabilities=["tool_calls"],
                    enabled_tools=["echo"],
                    mcp_tool_descriptor=descriptor,
                )
                task_id = task.id

            with self.Session() as session:
                claimed = run_task_worker_once(session, "worker-harness-output-limit")
                session.commit()
                self.assertEqual(claimed.status, "completed")

        function = handler.requests[0]["payload"]["tools"][0]["function"]
        self.assertLessEqual(len(function["description"]), 512)
        self.assertNotIn("x-agentic-policy", function["parameters"])
        tool_result = json.loads(handler.requests[1]["payload"]["messages"][-1]["content"])
        self.assertTrue(tool_result["truncated"])
        self.assertEqual(tool_result["output_limit_bytes"], 80)

        with self.Session() as session:
            event_types = set(
                session.execute(
                    select(AuditEvent.event_type).where(AuditEvent.task_id == task_id)
                ).scalars()
            )
            self.assertIn("tool.output_truncated", event_types)

    def test_worker_completes_task_through_harness_with_fake_endpoint(self) -> None:
        with fake_model_server("success") as (base_url, handler):
            with self.Session() as session:
                task = self._build_harness_task(session, base_url=base_url)
                task_id = task.id

            with self.Session() as session:
                claimed = run_task_worker_once(session, "worker-harness-success")
                session.commit()
                self.assertIsNotNone(claimed)
                self.assertEqual(claimed.status, "completed")

            self.assertEqual(len(handler.requests), 1)

        with self.Session() as session:
            task = session.get(Task, task_id)
            self.assertEqual(task.status, "completed")

            run = session.execute(select(Run).where(Run.task_id == task_id)).scalar_one()
            self.assertEqual(run.status, "completed")
            self.assertEqual(run.langgraph_thread_id, thread_id_for_task(task_id))

            event_types = [
                row.event_type
                for row in session.execute(
                    select(AuditEvent).where(AuditEvent.task_id == task_id).order_by(AuditEvent.sequence_number)
                ).scalars()
            ]
            self.assertIn("harness.invocation_started", event_types)
            self.assertIn("harness.invocation_completed", event_types)
            self.assertIn("harness.output_recorded", event_types)
            self.assertIn("run.completed", event_types)

    def test_unsupported_required_capability_fails_before_side_effects(self) -> None:
        with fake_model_server("success") as (base_url, handler):
            with self.Session() as session:
                task = self._build_harness_task(
                    session, base_url=base_url, required_capabilities=["tool_calls"]
                )
                task_id = task.id
                agent_version = session.get(AgentVersion, task.assigned_agent_version_id)
                probe = ModelProfileProbe(
                    model_profile_version_id=agent_version.model_profile_version_id,
                    status="degraded",
                    capability_evidence={
                        "tool_calls": {
                            "status": "unsupported",
                            "diagnostic": "provider returned HTTP 400",
                        }
                    },
                    pricing_evidence={"status": "valid", "metered": False, "warnings": [], "failures": []},
                    request_metadata={},
                    diagnostics=[],
                    started_at=datetime.now(timezone.utc),
                    completed_at=datetime.now(timezone.utc),
                )
                session.add(probe)
                session.commit()

            with self.Session() as session:
                with self.assertRaises(TaskExecutionError):
                    run_task_worker_once(session, "worker-harness-capability")
                session.commit()

            # The fail-closed capability check must happen before any network
            # call to the model endpoint.
            self.assertEqual(handler.requests, [])

        with self.Session() as session:
            task = session.get(Task, task_id)
            self.assertEqual(task.status, "failed")

            run = session.execute(select(Run).where(Run.task_id == task_id)).scalar_one()
            self.assertEqual(run.status, "failed")

            event_types = {
                row.event_type
                for row in session.execute(select(AuditEvent).where(AuditEvent.task_id == task_id)).scalars()
            }
            self.assertIn("harness.capability_check_failed", event_types)
            self.assertNotIn("harness.invocation_started", event_types)
            self.assertNotIn("artifact.output_published", event_types)

    def test_timeout_is_retried_and_then_succeeds(self) -> None:
        with fake_model_server("timeout_once") as (base_url, handler):
            with self.Session() as session:
                task = self._build_harness_task(session, base_url=base_url)
                # This test drives execute_model_harness directly, bypassing
                # claim_ready_task/run_task_worker_once, so the task must be
                # moved out of the claimable set itself -- otherwise a later
                # test's claim_ready_task (oldest-first) would pick it up and
                # dial this test's already-torn-down fake server.
                task.status = "completed"
                run = Run(
                    task_id=task.id,
                    attempt_number=1,
                    idempotency_key=f"{task.id}:1",
                    lease_token=0,
                    agent_version_id=task.assigned_agent_version_id,
                    status="running",
                    started_at=datetime.now(timezone.utc),
                )
                session.add(run)
                session.flush()

                goal_row = session.get(Goal, task.goal_id)
                project_row = session.get(Project, goal_row.project_id)
                resolved = resolve_run_configuration(session, task=task, run=run, project=project_row)
                context = CorrelationContext.for_run(project=project_row, goal=goal_row, task=task, run=run)

                result = execute_model_harness(
                    session,
                    task=task,
                    run=run,
                    project=project_row,
                    context=context,
                    model_profile=resolved.configuration["model_profile"],
                    instructions=resolved.configuration["agent"]["instructions"],
                    required_capabilities=[],
                    settings=HarnessSettings(timeout_seconds=0.05, max_attempts=2),
                )
                session.commit()

        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["content"], "probe response")
        self.assertGreaterEqual(handler.attempts, 2)

    def test_final_timeout_raises_harness_execution_error(self) -> None:
        with fake_model_server("timeout") as (base_url, _handler):
            with self.Session() as session:
                task = self._build_harness_task(session, base_url=base_url)
                # See test_timeout_is_retried_and_then_succeeds: keep this
                # task out of the claimable set for later tests.
                task.status = "completed"
                run = Run(
                    task_id=task.id,
                    attempt_number=1,
                    idempotency_key=f"{task.id}:1",
                    lease_token=0,
                    agent_version_id=task.assigned_agent_version_id,
                    status="running",
                    started_at=datetime.now(timezone.utc),
                )
                session.add(run)
                session.flush()

                goal_row = session.get(Goal, task.goal_id)
                project_row = session.get(Project, goal_row.project_id)
                resolved = resolve_run_configuration(session, task=task, run=run, project=project_row)
                context = CorrelationContext.for_run(project=project_row, goal=goal_row, task=task, run=run)

                with self.assertRaises(HarnessExecutionError):
                    execute_model_harness(
                        session,
                        task=task,
                        run=run,
                        project=project_row,
                        context=context,
                        model_profile=resolved.configuration["model_profile"],
                        instructions=None,
                        required_capabilities=[],
                        settings=HarnessSettings(timeout_seconds=0.05, max_attempts=2),
                    )
                session.commit()

    def test_restart_recovery_reuses_pinned_snapshot_and_thread_id(self) -> None:
        with fake_model_server("success") as (base_url, handler):
            with self.Session() as session:
                task = self._build_harness_task(session, base_url=base_url)
                task_id = task.id

            # Simulate a worker that claimed the task, resolved and pinned its
            # configuration snapshot (including the model profile), computed
            # the harness thread id, and then crashed before the model call
            # returned -- the run is left durably "running".
            with self.Session() as session:
                claimed_task = claim_ready_task(session, "worker-harness-crashed", lease_seconds=60)
                first_run = Run(
                    task_id=claimed_task.id,
                    attempt_number=1,
                    idempotency_key=f"{claimed_task.id}:1",
                    lease_token=claimed_task.lease_token,
                    agent_version_id=claimed_task.assigned_agent_version_id,
                    status="running",
                    langgraph_thread_id=thread_id_for_task(claimed_task.id),
                    started_at=datetime.now(timezone.utc) - timedelta(seconds=5),
                )
                session.add(first_run)
                session.flush()

                goal_row = session.get(Goal, claimed_task.goal_id)
                project_row = session.get(Project, goal_row.project_id)
                resolved = resolve_run_configuration(session, task=claimed_task, run=first_run, project=project_row)
                first_run.snapshot = {
                    "configuration_snapshot_id": str(resolved.snapshot_id),
                    "model_profile_version_id": resolved.configuration["model_profile"]["id"],
                }

                claimed_task.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                session.commit()
                first_run_id = first_run.id

            with self.Session() as session:
                claimed = run_task_worker_once(session, "worker-harness-recovered")
                session.commit()
                self.assertIsNotNone(claimed)
                self.assertEqual(claimed.status, "completed")

        with self.Session() as session:
            runs = list(
                session.execute(select(Run).where(Run.task_id == task_id).order_by(Run.attempt_number)).scalars()
            )
            self.assertEqual(len(runs), 2)
            first_run, second_run = runs
            self.assertEqual(first_run.id, first_run_id)
            self.assertEqual(first_run.status, "failed")
            self.assertEqual(second_run.status, "completed")

            self.assertEqual(second_run.langgraph_thread_id, thread_id_for_task(task_id))
            self.assertEqual(first_run.langgraph_thread_id, second_run.langgraph_thread_id)

            snapshots = list(
                session.execute(
                    select(RunConfigurationSnapshot)
                    .join(Run, RunConfigurationSnapshot.run_id == Run.id)
                    .where(Run.task_id == task_id)
                ).scalars()
            )
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(first_run.snapshot["configuration_snapshot_id"], str(snapshots[0].id))
            self.assertEqual(second_run.snapshot["configuration_snapshot_id"], str(snapshots[0].id))
            self.assertIsNotNone(second_run.snapshot["model_profile_version_id"])


if __name__ == "__main__":
    unittest.main()
