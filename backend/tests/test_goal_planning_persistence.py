from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sqlalchemy import create_engine, func, select

from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import (
    Agent,
    AgentVersion,
    AgentVersionMcpServer,
    AgentVersionSkill,
    AuditEvent,
    Budget,
    Goal,
    GoalPlanningSession,
    McpServer,
    McpServerTool,
    McpServerVersion,
    ModelProfileVersion,
    PlanningOverride,
    Policy,
    Project,
    Skill,
    SkillVersion,
    Team,
    User,
)
from agentic_os.domain.planning import (
    create_planning_session,
    get_planning_record,
    record_planning_override,
    update_planning_session,
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


class GoalPlanningPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_database_engine(TEST_DATABASE_URL)
        self.Session = session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _foundation(self, session):
        team = Team(name=f"Planning Team {uuid.uuid4()}")
        actor = User(
            email=f"planner-{uuid.uuid4()}@example.test",
            display_name="Planner",
        )
        session.add_all([team, actor])
        session.flush()
        project = Project(team_id=team.id, created_by=actor.id, name="Planning Project")
        session.add(project)
        session.flush()
        goal = Goal(project_id=project.id, created_by=actor.id, title="Plan this goal")
        session.add(goal)
        session.flush()
        return team, actor, project, goal

    def _candidate_versions(self, session, team, actor):
        model = ModelProfileVersion(
            model_profile_id=self._model_profile(session, team, actor).id,
            version_number=1,
            base_url="https://models.example.test/v1",
            model_identifier="planner-model",
            headers={"Authorization": "Bearer do-not-store"},
            capability_metadata={"tool_calling": True, "api_key": "do-not-store"},
            pricing_metadata={"input_tokens": 1, "secret_rate": "do-not-store"},
        )
        session.add(model)
        session.flush()
        selected_agent = Agent(team_id=team.id, created_by=actor.id, name="Selected Agent")
        rejected_agent = Agent(team_id=team.id, created_by=actor.id, name="Rejected Agent")
        session.add_all([selected_agent, rejected_agent])
        session.flush()
        budget = Budget(
            agent_id=selected_agent.id,
            currency="USD",
            amount_minor_units=5000,
            enforcement_mode="hard_stop",
        )
        session.add(budget)
        session.flush()
        selected = AgentVersion(
            agent_id=selected_agent.id,
            version_number=1,
            capability_manifest={
                "capabilities": ["research"],
                "secret_token": "do-not-store",
            },
            model_profile_version_id=model.id,
            default_budget_id=budget.id,
        )
        rejected = AgentVersion(
            agent_id=rejected_agent.id,
            version_number=1,
            capability_manifest={"capabilities": ["writing"]},
        )
        session.add_all([selected, rejected])
        session.flush()
        return selected, rejected

    def _model_profile(self, session, team, actor):
        from agentic_os.domain.models import ModelProfile

        profile = ModelProfile(
            team_id=team.id,
            created_by=actor.id,
            name="Planner Model",
            base_url="https://models.example.test/v1",
            model_identifier="planner-model",
            api_key_ciphertext="encrypted",
        )
        session.add(profile)
        session.flush()
        return profile

    def test_planning_records_persist_queryable_redacted_evidence_and_audit(self) -> None:
        with self.Session() as session:
            team, actor, project, goal = self._foundation(session)
            selected, rejected = self._candidate_versions(session, team, actor)
            skill = Skill(team_id=team.id, created_by=actor.id, name="Research Skill")
            session.add(skill)
            session.flush()
            skill_version = SkillVersion(
                skill_id=skill.id,
                version_number=1,
                content_ref="skills/research/v1",
                declared_capabilities=["research"],
                resources=[{"path": "guide.md", "api_key": "do-not-store"}],
            )
            session.add(skill_version)
            session.flush()
            session.add(
                AgentVersionSkill(
                    agent_version_id=selected.id,
                    skill_version_id=skill_version.id,
                    attachment_config={"enabled": True, "secret": "do-not-store"},
                    granted_by=actor.id,
                )
            )
            mcp = McpServer(
                project_id=project.id,
                created_by=actor.id,
                name="Search MCP",
            )
            session.add(mcp)
            session.flush()
            mcp_version = McpServerVersion(
                mcp_server_id=mcp.id,
                version_number=1,
                connection_config={"url": "https://mcp.example.test", "token": "do-not-store"},
                credential_ciphertext="encrypted",
            )
            session.add(mcp_version)
            session.flush()
            session.add(
                AgentVersionMcpServer(
                    agent_version_id=selected.id,
                    mcp_server_version_id=mcp_version.id,
                    attachment_config={"enabled": True, "password": "do-not-store"},
                    granted_by=actor.id,
                )
            )
            session.add(
                McpServerTool(
                    mcp_server_version_id=mcp_version.id,
                    tool_name="search",
                    descriptor_hash="hash",
                    schema_valid=True,
                    enabled=True,
                    timeout_ms=1000,
                )
            )
            session.add(
                Policy(
                    scope_type="goal",
                    scope_id=goal.id,
                    decision="allow",
                    rule={"network": "allow", "authorization": "do-not-store"},
                )
            )
            session.flush()

            planning = create_planning_session(
                session,
                goal_id=goal.id,
                actor_id=actor.id,
                requirements=[
                    {
                        "capability_key": "research",
                        "rationale": "Goal needs sources",
                        "source_evidence": {"goal": "research", "api_key": "do-not-store"},
                    }
                ],
                candidates=[
                    {
                        "agent_version_id": selected.id,
                        "eligible": True,
                        "matched_capabilities": ["research"],
                        "evidence": {"score": 1, "secret": "do-not-store"},
                    },
                    {
                        "agent_version_id": rejected.id,
                        "eligible": False,
                        "missing_capabilities": ["research"],
                        "rejection_reasons": ["missing_capability:research"],
                    },
                ],
                assignments=[
                    {
                        "assignment_key": "research-task",
                        "capability_key": "research",
                        "agent_version_id": selected.id,
                        "rationale": "Only eligible candidate",
                        "validation_status": "valid",
                    }
                ],
                constraints={"policy": "current", "api_key": "do-not-store"},
            )
            planning_id = planning.id
            goal_id = goal.id
            session.commit()

        with self.Session() as session:
            record = get_planning_record(session, planning_id)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record["requirements"][0]["capability_key"], "research")
            self.assertEqual(
                record["candidates"][1]["rejection_reasons"],
                ["missing_capability:research"],
            )
            snapshot = record["candidates"][0]["constraints_snapshot"]
            self.assertEqual(snapshot["agent_manifest"]["secret_token"], "[REDACTED]")
            self.assertEqual(snapshot["skills"][0]["resources"][0]["api_key"], "[REDACTED]")
            self.assertEqual(snapshot["mcp_servers"][0]["enabled_tools"][0]["name"], "search")
            self.assertTrue(snapshot["mcp_servers"][0]["credential_configured"])
            self.assertEqual(snapshot["model"]["capability_metadata"]["api_key"], "[REDACTED]")
            self.assertEqual(snapshot["policies"][0]["rule"]["authorization"], "[REDACTED]")
            serialized = str(record)
            self.assertNotIn("do-not-store", serialized)
            audit = session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "goal.planning_session_created",
                    AuditEvent.goal_id == goal_id,
                )
            ).scalar_one()
            self.assertEqual(audit.payload["planning_session_id"], str(planning_id))

    def test_valid_override_preserves_prior_evidence_and_updates_assignment(self) -> None:
        with self.Session() as session:
            team, actor, _project, goal = self._foundation(session)
            first, second = self._candidate_versions(session, team, actor)
            planning = create_planning_session(
                session,
                goal_id=goal.id,
                actor_id=actor.id,
                requirements=[{"capability_key": "research"}],
                candidates=[
                    {"agent_version_id": first.id, "eligible": True, "evidence": {"rank": 1}},
                    {"agent_version_id": second.id, "eligible": True, "evidence": {"rank": 2}},
                ],
                assignments=[
                    {
                        "assignment_key": "task-a",
                        "capability_key": "research",
                        "agent_version_id": first.id,
                    }
                ],
            )
            override = record_planning_override(
                session,
                planning_session_id=planning.id,
                assignment_key="task-a",
                actor_id=actor.id,
                requested_agent_version_id=second.id,
                reason="Operator prefers the second candidate",
                validation_status="valid",
                validation_evidence={"policy": "allow", "secret": "do-not-store"},
            )
            update_planning_session(
                session,
                planning_session_id=planning.id,
                actor_id=actor.id,
                status="accepted",
                validation_status="valid",
            )
            planning_id = planning.id
            override_id = override.id
            session.commit()

        with self.Session() as session:
            record = get_planning_record(session, planning_id)
            assert record is not None
            self.assertEqual(len(record["overrides"]), 1)
            self.assertEqual(record["status"], "accepted")
            self.assertEqual(record["overrides"][0]["id"], str(override_id))
            self.assertEqual(
                record["overrides"][0]["prior_candidate_evidence"]["agent_version_id"],
                str(first.id),
            )
            self.assertEqual(
                record["overrides"][0]["validation_evidence"]["secret"],
                "[REDACTED]",
            )
            self.assertEqual(
                record["assignments"][0]["candidate_id"],
                record["candidates"][1]["id"],
            )
            persisted = session.get(PlanningOverride, override_id)
            self.assertEqual(persisted.actor_id, actor.id)

    def test_invalid_cross_team_candidate_rolls_back_entire_planning_record(self) -> None:
        with self.Session() as session:
            _team, actor, _project, goal = self._foundation(session)
            other_team = Team(name=f"Other Team {uuid.uuid4()}")
            session.add(other_team)
            session.flush()
            other_agent = Agent(
                team_id=other_team.id,
                created_by=actor.id,
                name="Out-of-team Agent",
            )
            session.add(other_agent)
            session.flush()
            other_version = AgentVersion(
                agent_id=other_agent.id,
                version_number=1,
                capability_manifest={"capabilities": ["research"]},
            )
            session.add(other_version)
            session.flush()
            session.commit()

            with self.assertRaisesRegex(ValueError, "outside goal project team"):
                create_planning_session(
                    session,
                    goal_id=goal.id,
                    actor_id=actor.id,
                    requirements=[{"capability_key": "research"}],
                    candidates=[
                        {"agent_version_id": other_version.id, "eligible": True}
                    ],
                )
            session.rollback()

            count = session.execute(
                select(func.count()).select_from(GoalPlanningSession).where(
                    GoalPlanningSession.goal_id == goal.id
                )
            ).scalar_one()
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
