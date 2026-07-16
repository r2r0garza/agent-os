from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sqlalchemy import create_engine, func, select

from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import (
    ApprovalRequest,
    ProjectMember,
    Run,
    Team,
    TeamMembership,
    User,
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
        ["alembic", "upgrade", "head"], cwd=BACKEND_ROOT,
        env=dict(os.environ, AGENTIC_OS_DATABASE_URL=db_url), check=True,
        capture_output=True, text=True,
    )
    os.environ["AGENTIC_OS_DATABASE_URL"] = db_url
    from fastapi.testclient import TestClient
    from agentic_os.api.app import create_app

    client = TestClient(create_app())
    SessionLocal = session_factory(create_database_engine(db_url))


class GovernanceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = client.post("/api/v1/projects", json={"name": f"Governance {uuid.uuid4()}"}).json()
        self.goal = client.post(
            f"/api/v1/projects/{self.project['id']}/goals", json={"title": "Govern this"}
        ).json()
        self.agent = client.post("/api/v1/agents", json={"name": f"Agent {uuid.uuid4()}"}).json()
        self.agent_version = client.post(
            f"/api/v1/agents/{self.agent['id']}/versions",
            json={"instructions": "Governed worker", "capability_manifest": {}},
        ).json()
        graph = client.post(
            f"/api/v1/goals/{self.goal['id']}/task-graph",
            json={"tasks": [{"client_id": "task", "title": "Governed task"}]},
        ).json()
        self.task = graph["tasks"][0]

    def _configure(self) -> dict:
        response = client.post(
            f"/api/v1/projects/{self.project['id']}/approval-mode-configurations",
            json={
                "mode": "consequential",
                "consequential_action_types": ["tool.publish"],
                "context": {"api_token": "must-not-return", "source": "operator"},
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _request(self, configuration: dict, *, expires_at: datetime | None = None) -> dict:
        with SessionLocal.begin() as session:
            attempt_number = session.execute(
                select(func.count(Run.id)).where(Run.task_id == uuid.UUID(self.task["id"]))
            ).scalar_one() + 1
            run = Run(
                task_id=uuid.UUID(self.task["id"]), attempt_number=attempt_number,
                idempotency_key=f"approval-{uuid.uuid4()}", lease_token=1,
                agent_version_id=uuid.UUID(self.agent_version["id"]), status="waiting_approval",
            )
            session.add(run)
            session.flush()
            team = session.execute(select(Team).where(Team.name == "Default Team")).scalar_one()
            request = ApprovalRequest(
                team_id=team.id, project_id=uuid.UUID(self.project["id"]),
                goal_id=uuid.UUID(self.goal["id"]), task_id=uuid.UUID(self.task["id"]),
                run_id=run.id, agent_version_id=uuid.UUID(self.agent_version["id"]),
                configuration_id=uuid.UUID(configuration["id"]), mode="consequential",
                action_type="tool.publish",
                action_preview={"destination": "public", "authorization": "must-not-return"},
                policy_version_ids=[], policy_evidence={"decision": "approval_required", "secret": "hidden"},
                expires_at=expires_at,
            )
            session.add(request)
            session.flush()
            return {"id": str(request.id), "run_id": str(run.id)}

    def _regular_user(self, *, project_access: bool) -> User:
        with SessionLocal.begin() as session:
            team = session.execute(select(Team).where(Team.name == "Default Team")).scalar_one()
            user = User(
                email=f"regular-{uuid.uuid4()}@example.test", display_name="Regular", role="regular_user"
            )
            session.add(user)
            session.flush()
            session.add(TeamMembership(team_id=team.id, user_id=user.id))
            if project_access:
                session.add(ProjectMember(project_id=uuid.UUID(self.project["id"]), user_id=user.id))
            session.flush()
            session.expunge(user)
            return user

    def test_configures_versioned_modes_validates_and_redacts_context(self) -> None:
        first = self._configure()
        self.assertEqual(first["version_number"], 1)
        self.assertEqual(first["context"]["api_token"], "[REDACTED]")
        second = client.post(
            f"/api/v1/projects/{self.project['id']}/approval-mode-configurations",
            json={"mode": "every_tool_call"},
        )
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(second.json()["version_number"], 2)
        invalid = client.post(
            f"/api/v1/projects/{self.project['id']}/approval-mode-configurations",
            json={"mode": "consequential"},
        )
        self.assertEqual(invalid.status_code, 422)
        listed = client.get(
            f"/api/v1/projects/{self.project['id']}/approval-mode-configurations"
        ).json()
        self.assertEqual([item["version_number"] for item in listed], [1, 2])
        self.assertEqual(
            client.get(f"/api/v1/approval-mode-configurations/{first['id']}").json()["id"],
            first["id"],
        )

    def test_authorized_regular_user_lists_reads_and_resolves_redacted_requests(self) -> None:
        configuration = self._configure()
        approval = self._request(configuration)
        regular = self._regular_user(project_access=True)
        headers = {"X-Agentic-User-ID": str(regular.id)}
        listed = client.get(
            "/api/v1/approval-requests",
            params={"project_id": self.project["id"], "status": "pending"},
            headers=headers,
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()[0]["action_preview"]["authorization"], "[REDACTED]")
        self.assertEqual(listed.json()[0]["policy_evidence"]["secret"], "[REDACTED]")
        detail = client.get(f"/api/v1/approval-requests/{approval['id']}", headers=headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        resolved = client.post(
            f"/api/v1/approval-requests/{approval['id']}/approve",
            json={"reason": "Reviewed", "context": {"access_token": "hidden"}}, headers=headers,
        )
        self.assertEqual(resolved.status_code, 201, resolved.text)
        self.assertEqual(resolved.json()["decision"], "approved")
        self.assertEqual(resolved.json()["context"]["access_token"], "[REDACTED]")
        duplicate = client.post(
            f"/api/v1/approval-requests/{approval['id']}/deny", json={}, headers=headers
        )
        self.assertEqual(duplicate.status_code, 409)

    def test_deny_and_expire_operations_persist_decisions(self) -> None:
        configuration = self._configure()
        denied = self._request(configuration)
        expired = self._request(configuration, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        self.assertEqual(
            client.post(f"/api/v1/approval-requests/{denied['id']}/deny", json={}).json()["decision"],
            "denied",
        )
        self.assertEqual(
            client.post(f"/api/v1/approval-requests/{expired['id']}/expire", json={}).json()["decision"],
            "expired",
        )

    def test_role_and_ownership_boundaries_protect_requests_and_overrides(self) -> None:
        configuration = self._configure()
        approval = self._request(configuration)
        outsider = self._regular_user(project_access=False)
        outsider_headers = {"X-Agentic-User-ID": str(outsider.id)}
        self.assertEqual(
            client.get(f"/api/v1/approval-requests/{approval['id']}", headers=outsider_headers).status_code,
            403,
        )
        override_payload = {
            "scope_type": "run", "scope_id": approval["run_id"], "reason": "Incident response",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "context": {"api_key": "hidden"},
        }
        member = self._regular_user(project_access=True)
        self.assertEqual(
            client.post(
                "/api/v1/admin-overrides", json=override_payload,
                headers={"X-Agentic-User-ID": str(member.id)},
            ).status_code,
            403,
        )
        created = client.post("/api/v1/admin-overrides", json=override_payload)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["context"]["api_key"], "[REDACTED]")
        self.assertEqual(len(client.get(
            "/api/v1/admin-overrides", params={"project_id": self.project["id"]}
        ).json()), 1)
        self.assertEqual(
            client.get(f"/api/v1/admin-overrides/{created.json()['id']}").json()["id"],
            created.json()["id"],
        )

    def test_governance_evidence_is_scoped_and_redacted(self) -> None:
        configuration = self._configure()
        approval = self._request(configuration)
        client.post(f"/api/v1/approval-requests/{approval['id']}/approve", json={"reason": "Safe"})
        response = client.get("/api/v1/governance/evidence", params={"run_id": approval["run_id"]})
        self.assertEqual(response.status_code, 200, response.text)
        evidence = response.json()
        self.assertEqual(len(evidence["approval_requests"]), 1)
        self.assertEqual(evidence["approval_requests"][0]["action_preview"]["authorization"], "[REDACTED]")
        self.assertEqual(len(evidence["approval_decisions"]), 1)
        self.assertTrue(any(item["event_type"] == "approval.approved" for item in evidence["audit_events"]))

    def test_budget_update_validates_governance_settings(self) -> None:
        budget = client.post(
            f"/api/v1/agents/{self.agent['id']}/budgets",
            json={"currency": "USD", "amount_minor_units": 100, "enforcement_mode": "warning"},
        ).json()
        updated = client.patch(
            f"/api/v1/budgets/{budget['id']}",
            json={"amount_minor_units": 200, "enforcement_mode": "hard_stop", "warning_threshold_percent": 80},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["amount_minor_units"], 200)
        self.assertEqual(client.patch(
            f"/api/v1/budgets/{budget['id']}", json={"warning_threshold_percent": 101}
        ).status_code, 422)


if __name__ == "__main__":
    unittest.main()
