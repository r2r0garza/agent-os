from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from sqlalchemy import create_engine

from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import (
    Agent,
    AgentVersion,
    AgentVersionMcpServer,
    AgentVersionSkill,
    Budget,
    McpServer,
    McpServerHealthCheck,
    McpServerTool,
    McpServerVersion,
    ModelProfile,
    ModelProfileVersion,
    Policy,
    Skill,
    SkillVersion,
    Task,
)
from factories import (
    make_goal,
    make_project,
    make_project_member,
    make_team,
    make_team_membership,
    make_user,
)

BACKEND_ROOT = Path(__file__).parents[1]


def setUpModule() -> None:
    global client, SessionLocal
    db_url = os.environ.get("AGENTIC_OS_DATABASE_URL", database_url())
    probe = create_database_engine(db_url)
    try:
        with probe.connect():
            pass
    except Exception as error:  # pragma: no cover - environment guard
        raise unittest.SkipTest(f"PostgreSQL is not reachable: {error}")
    finally:
        probe.dispose()
    engine = create_engine(db_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    engine.dispose()
    subprocess.run(
        [str(BACKEND_ROOT / ".venv" / "bin" / "alembic"), "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=dict(os.environ, AGENTIC_OS_DATABASE_URL=db_url),
        check=True,
        capture_output=True,
        text=True,
    )
    os.environ["AGENTIC_OS_DATABASE_URL"] = db_url

    from agentic_os.api.deps import _engine

    if _engine.cache_info().currsize:
        _engine().dispose()
        _engine.cache_clear()
    from fastapi.testclient import TestClient

    from agentic_os.api.app import create_app

    client = TestClient(create_app())
    SessionLocal = session_factory(create_database_engine(db_url))


class GoalPlanningApiTests(unittest.TestCase):
    def setUp(self) -> None:
        with SessionLocal.begin() as session:
            self.team = make_team(session, name=f"Planning team {uuid.uuid4()}")
            self.owner = make_user(session, display_name="Owner")
            make_team_membership(session, self.team, self.owner, role="owner")
            self.outsider = make_user(session, display_name="Outsider")
            other_team = make_team(session, name=f"Other team {uuid.uuid4()}")
            make_team_membership(session, other_team, self.outsider)

            self.project = make_project(session, self.team, self.owner, name="Planning project")
            make_project_member(session, self.project, self.owner, granted_by=self.owner)
            self.goal = make_goal(session, self.project, self.owner, title="Ship the plan", status="active")

            self.eligible_agent = Agent(team_id=self.team.id, created_by=self.owner.id, name="Eligible Agent")
            self.ineligible_agent = Agent(team_id=self.team.id, created_by=self.owner.id, name="Ineligible Agent")
            session.add_all([self.eligible_agent, self.ineligible_agent])
            session.flush()
            self.eligible_version = AgentVersion(
                agent_id=self.eligible_agent.id,
                version_number=1,
                capability_manifest={"capabilities": ["research"]},
            )
            self.ineligible_version = AgentVersion(
                agent_id=self.ineligible_agent.id,
                version_number=1,
                capability_manifest={"capabilities": ["writing"]},
            )
            session.add_all([self.eligible_version, self.ineligible_version])
            session.flush()

            self.task = Task(
                goal_id=self.goal.id,
                created_by=self.owner.id,
                title="Research the topic",
                required_capabilities={"research": True},
            )
            session.add(self.task)
            session.flush()

            for value in (
                self.team,
                self.owner,
                self.outsider,
                self.project,
                self.goal,
                self.eligible_agent,
                self.ineligible_agent,
                self.eligible_version,
                self.ineligible_version,
                self.task,
            ):
                session.expunge(value)

    @staticmethod
    def _headers(actor) -> dict[str, str]:
        return {"X-Agentic-User-ID": str(actor.id)}

    def _preview_payload(self, *, assignment_agent_version_id=None) -> dict:
        assignments = []
        if assignment_agent_version_id is not None:
            assignments.append(
                {
                    "assignment_key": str(self.task.id),
                    "capability_key": "research",
                    "agent_version_id": str(assignment_agent_version_id),
                    "rationale": "Only eligible candidate",
                }
            )
        return {
            "requirements": [{"capability_key": "research", "rationale": "Goal needs sources"}],
            "candidates": [
                {"agent_version_id": str(self.eligible_version.id)},
                {"agent_version_id": str(self.ineligible_version.id)},
            ],
            "assignments": assignments,
        }

    def test_preview_computes_eligibility_and_persists_evidence(self) -> None:
        response = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions",
            json=self._preview_payload(assignment_agent_version_id=self.eligible_version.id),
            headers=self._headers(self.owner),
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["status"], "previewed")
        self.assertEqual(body["validation_status"], "valid")
        self.assertEqual(len(body["candidates"]), 2)
        by_version = {item["agent_version_id"]: item for item in body["candidates"]}
        self.assertTrue(by_version[str(self.eligible_version.id)]["eligible"])
        self.assertFalse(by_version[str(self.ineligible_version.id)]["eligible"])
        self.assertEqual(
            by_version[str(self.ineligible_version.id)]["rejection_reasons"],
            ["missing_capability:research"],
        )
        self.assertEqual(body["assignments"][0]["validation_status"], "valid")

        fetched = client.get(
            f"/api/v1/goals/{self.goal.id}/planning-sessions/{body['id']}",
            headers=self._headers(self.owner),
        )
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json(), body)

    def test_preview_derives_requirements_discovers_candidates_and_assigns(self) -> None:
        response = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions",
            json={},
            headers=self._headers(self.owner),
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["validation_status"], "valid")
        self.assertEqual(
            [item["capability_key"] for item in body["requirements"]],
            ["research"],
        )
        self.assertEqual(len(body["assignments"]), 1)
        self.assertEqual(body["assignments"][0]["assignment_key"], str(self.task.id))
        selected = next(item for item in body["candidates"] if item["eligible"])
        selected_rank = selected["evidence"]["selection_rank"]
        self.assertEqual(selected_rank, 1)
        self.assertEqual(
            body["assignments"][0]["candidate_id"],
            selected["id"],
        )

    def test_preview_forms_team_across_task_specific_capabilities(self) -> None:
        with SessionLocal.begin() as session:
            writing_task = Task(
                goal_id=self.goal.id,
                created_by=self.owner.id,
                title="Write the findings",
                required_capabilities={"writing": True},
            )
            session.add(writing_task)
            session.flush()
            writing_task_id = writing_task.id

        response = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions",
            json={},
            headers=self._headers(self.owner),
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(
            {item["capability_key"] for item in body["requirements"]},
            {"research", "writing"},
        )
        candidates = {
            item["id"]: item["agent_version_id"] for item in body["candidates"]
        }
        assignments = {
            item["assignment_key"]: candidates[item["candidate_id"]]
            for item in body["assignments"]
        }
        self.assertEqual(
            assignments[str(self.task.id)],
            str(self.eligible_version.id),
        )
        self.assertEqual(
            assignments[str(writing_task_id)],
            str(self.ineligible_version.id),
        )

    def test_preview_rejects_assignment_to_ineligible_candidate(self) -> None:
        response = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions",
            json=self._preview_payload(assignment_agent_version_id=self.ineligible_version.id),
            headers=self._headers(self.owner),
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("ineligible candidate", response.json()["detail"])

    def test_preview_rejects_unauthorized_actor(self) -> None:
        response = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions",
            json=self._preview_payload(),
            headers=self._headers(self.outsider),
        )
        self.assertEqual(response.status_code, 404)

    def test_preview_rejects_unknown_agent_version(self) -> None:
        payload = self._preview_payload()
        payload["candidates"].append({"agent_version_id": str(uuid.uuid4())})
        response = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions",
            json=payload,
            headers=self._headers(self.owner),
        )
        self.assertEqual(response.status_code, 422)

    def test_override_replaces_assignment_when_eligible(self) -> None:
        preview = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions",
            json=self._preview_payload(assignment_agent_version_id=self.eligible_version.id),
            headers=self._headers(self.owner),
        ).json()

        second_eligible_agent_response = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions/{preview['id']}/overrides",
            json={
                "assignment_key": str(self.task.id),
                "agent_version_id": str(self.eligible_version.id),
                "reason": "Re-confirm the same eligible candidate",
            },
            headers=self._headers(self.owner),
        )
        self.assertEqual(second_eligible_agent_response.status_code, 201, second_eligible_agent_response.text)
        body = second_eligible_agent_response.json()
        self.assertEqual(len(body["overrides"]), 1)
        self.assertEqual(body["overrides"][0]["validation_status"], "valid")

    def test_override_rejects_ineligible_candidate_but_persists_audit_trail(self) -> None:
        preview = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions",
            json=self._preview_payload(assignment_agent_version_id=self.eligible_version.id),
            headers=self._headers(self.owner),
        ).json()

        response = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions/{preview['id']}/overrides",
            json={
                "assignment_key": str(self.task.id),
                "agent_version_id": str(self.ineligible_version.id),
                "reason": "Operator tries an ineligible candidate",
            },
            headers=self._headers(self.owner),
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"]["rejection_reasons"],
            ["missing_capability:research"],
        )

        fetched = client.get(
            f"/api/v1/goals/{self.goal.id}/planning-sessions/{preview['id']}",
            headers=self._headers(self.owner),
        ).json()
        self.assertEqual(len(fetched["overrides"]), 1)
        self.assertEqual(fetched["overrides"][0]["validation_status"], "invalid")
        self.assertEqual(fetched["assignments"][0]["candidate_id"], fetched["candidates"][0]["id"])

    def test_accept_materializes_task_assignment_and_is_idempotent(self) -> None:
        preview = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions",
            json=self._preview_payload(assignment_agent_version_id=self.eligible_version.id),
            headers=self._headers(self.owner),
        ).json()

        accept_response = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions/{preview['id']}/accept",
            headers=self._headers(self.owner),
        )
        self.assertEqual(accept_response.status_code, 200, accept_response.text)
        accepted_body = accept_response.json()
        self.assertEqual(accepted_body["status"], "accepted")
        self.assertEqual(len(accepted_body["materialized_tasks"]), 1)
        self.assertEqual(accepted_body["materialized_tasks"][0]["task_id"], str(self.task.id))

        with SessionLocal() as session:
            task = session.get(Task, self.task.id)
            self.assertEqual(task.assigned_agent_version_id, self.eligible_version.id)
            self.assertEqual(task.assignment_status, "assigned")

        second_accept = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions/{preview['id']}/accept",
            headers=self._headers(self.owner),
        )
        self.assertEqual(second_accept.status_code, 200)
        self.assertEqual(second_accept.json()["status"], "accepted")
        self.assertEqual(second_accept.json()["materialized_tasks"], [])

    def test_accept_rejects_unresolved_assignment(self) -> None:
        payload = self._preview_payload()
        payload["constraints"] = {
            "required_model_capabilities": ["tool_calling"],
        }
        preview = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions",
            json=payload,
            headers=self._headers(self.owner),
        ).json()
        self.assertEqual(preview["validation_status"], "pending")

        response = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions/{preview['id']}/accept",
            headers=self._headers(self.owner),
        )
        self.assertEqual(response.status_code, 422)

    def test_preview_uses_enabled_skill_capabilities(self) -> None:
        with SessionLocal.begin() as session:
            skill = Skill(
                team_id=self.team.id,
                created_by=self.owner.id,
                name=f"Research skill {uuid.uuid4()}",
            )
            session.add(skill)
            session.flush()
            skill_version = SkillVersion(
                skill_id=skill.id,
                version_number=1,
                content_ref="skills/research/v1",
                declared_capabilities=["research"],
            )
            session.add(skill_version)
            session.flush()
            session.add(
                AgentVersionSkill(
                    agent_version_id=self.ineligible_version.id,
                    skill_version_id=skill_version.id,
                    attachment_config={"enabled": True},
                    granted_by=self.owner.id,
                )
            )

        response = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions",
            json=self._preview_payload(),
            headers=self._headers(self.owner),
        )
        self.assertEqual(response.status_code, 201, response.text)
        by_version = {
            item["agent_version_id"]: item for item in response.json()["candidates"]
        }
        skill_candidate = by_version[str(self.ineligible_version.id)]
        self.assertTrue(skill_candidate["eligible"])
        self.assertIn("research", skill_candidate["matched_capabilities"])
        self.assertEqual(
            skill_candidate["evidence"]["skill_grants"][0][
                "declared_capabilities"
            ],
            ["research"],
        )

    def test_preview_requires_healthy_enabled_mcp_and_compatible_model(self) -> None:
        with SessionLocal.begin() as session:
            model = ModelProfile(
                team_id=self.team.id,
                created_by=self.owner.id,
                name=f"Planning model {uuid.uuid4()}",
                base_url="https://models.example.test/v1",
                model_identifier="planner",
                api_key_ciphertext="encrypted",
            )
            session.add(model)
            session.flush()
            model_version = ModelProfileVersion(
                model_profile_id=model.id,
                version_number=1,
                base_url=model.base_url,
                model_identifier=model.model_identifier,
                capability_metadata={"tool_calling": True},
            )
            session.add(model_version)
            mcp = McpServer(
                project_id=self.project.id,
                created_by=self.owner.id,
                name=f"Search MCP {uuid.uuid4()}",
            )
            session.add(mcp)
            session.flush()
            mcp_version = McpServerVersion(
                mcp_server_id=mcp.id,
                version_number=1,
                connection_config={},
            )
            session.add(mcp_version)
            session.flush()
            session.add_all(
                [
                    AgentVersionMcpServer(
                        agent_version_id=self.eligible_version.id,
                        mcp_server_version_id=mcp_version.id,
                        attachment_config={"enabled": True},
                        granted_by=self.owner.id,
                    ),
                    McpServerTool(
                        mcp_server_version_id=mcp_version.id,
                        tool_name="search",
                        schema_valid=True,
                        descriptor_hash="search-v1",
                        enabled=True,
                    ),
                    McpServerHealthCheck(
                        mcp_server_version_id=mcp_version.id,
                        status="degraded",
                        triggered_by=self.owner.id,
                    ),
                ]
            )
            version = session.get(AgentVersion, self.eligible_version.id)
            version.model_profile_version_id = model_version.id
            session.flush()
            mcp_version_id = mcp_version.id

        payload = self._preview_payload()
        payload["constraints"] = {
            "required_tools": ["search"],
            "required_model_capabilities": ["tool_calling"],
        }
        degraded = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions",
            json=payload,
            headers=self._headers(self.owner),
        )
        self.assertEqual(degraded.status_code, 201, degraded.text)
        by_version = {
            item["agent_version_id"]: item
            for item in degraded.json()["candidates"]
        }
        self.assertIn(
            "mcp_health_degraded:search",
            by_version[str(self.eligible_version.id)]["rejection_reasons"],
        )

        with SessionLocal.begin() as session:
            session.add(
                McpServerHealthCheck(
                    mcp_server_version_id=mcp_version_id,
                    status="healthy",
                    triggered_by=self.owner.id,
                )
            )
        healthy = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions",
            json=payload,
            headers=self._headers(self.owner),
        )
        self.assertEqual(healthy.status_code, 201, healthy.text)
        by_version = {
            item["agent_version_id"]: item for item in healthy.json()["candidates"]
        }
        self.assertTrue(by_version[str(self.eligible_version.id)]["eligible"])

    def test_preview_rejects_policy_denial_and_exhausted_default_budget(self) -> None:
        with SessionLocal.begin() as session:
            budget = Budget(
                agent_id=self.eligible_agent.id,
                currency="USD",
                amount_minor_units=0,
                enforcement_mode="hard_stop",
            )
            session.add(budget)
            session.flush()
            version = session.get(AgentVersion, self.eligible_version.id)
            version.default_budget_id = budget.id
            session.add(
                Policy(
                    scope_type="agent",
                    scope_id=self.ineligible_agent.id,
                    decision="deny",
                    rule={},
                )
            )
            session.flush()
            budget_id = budget.id

        response = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions",
            json=self._preview_payload(),
            headers=self._headers(self.owner),
        )
        self.assertEqual(response.status_code, 201, response.text)
        by_version = {
            item["agent_version_id"]: item for item in response.json()["candidates"]
        }
        self.assertIn(
            f"budget_exhausted:{budget_id}",
            by_version[str(self.eligible_version.id)]["rejection_reasons"],
        )
        self.assertIn(
            f"agent_policy_deny:{self.ineligible_agent.id}",
            by_version[str(self.ineligible_version.id)]["rejection_reasons"],
        )

    def test_preview_evaluates_budget_and_tool_constraints(self) -> None:
        with SessionLocal.begin() as session:
            budget = Budget(
                agent_id=self.eligible_agent.id,
                currency="USD",
                amount_minor_units=100,
                enforcement_mode="hard_stop",
            )
            session.add(budget)
            session.flush()
            budget_id = budget.id
            session.expunge(budget)

        payload = self._preview_payload()
        payload["constraints"] = {
            "budget_id": str(budget_id),
            "required_tools": ["search"],
        }
        response = client.post(
            f"/api/v1/goals/{self.goal.id}/planning-sessions",
            json=payload,
            headers=self._headers(self.owner),
        )
        self.assertEqual(response.status_code, 201, response.text)
        by_version = {item["agent_version_id"]: item for item in response.json()["candidates"]}
        eligible_candidate = by_version[str(self.eligible_version.id)]
        self.assertFalse(eligible_candidate["eligible"])
        self.assertIn("mcp_tool_disabled_or_missing:search", eligible_candidate["rejection_reasons"])
        ineligible_candidate = by_version[str(self.ineligible_version.id)]
        self.assertIn(
            f"budget_belongs_to_other_agent:{budget_id}",
            ineligible_candidate["rejection_reasons"],
        )


if __name__ == "__main__":
    unittest.main()
