from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from sqlalchemy import create_engine, select

from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import (
    GoalLifecycleEvent,
    GoalSteeringRequest,
    Task,
    TaskDependency,
    TaskGraphRevision,
    TaskGraphRevisionTask,
)
from factories import (
    make_goal,
    make_project,
    make_project_member,
    make_task_graph_revision,
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


class GoalLifecycleApiTests(unittest.TestCase):
    def setUp(self) -> None:
        with SessionLocal.begin() as session:
            self.team = make_team(session, name=f"Lifecycle team {uuid.uuid4()}")
            self.owner = make_user(session, display_name="Owner")
            make_team_membership(session, self.team, self.owner, role="owner")
            self.outsider = make_user(session, display_name="Outsider")
            other_team = make_team(session, name=f"Other team {uuid.uuid4()}")
            make_team_membership(session, other_team, self.outsider)
            self.admin = make_user(session, display_name="Admin", role="admin")

            self.project = make_project(session, self.team, self.owner, name="Lifecycle project")
            make_project_member(session, self.project, self.owner, granted_by=self.owner)
            self.goal = make_goal(session, self.project, self.owner, title="Ship it", status="active")

            for value in (self.team, self.owner, self.outsider, self.admin, self.project, self.goal):
                session.expunge(value)

    @staticmethod
    def _headers(actor) -> dict[str, str]:
        return {"X-Agentic-User-ID": str(actor.id)}

    def test_pause_resume_cancel_lifecycle_transitions(self) -> None:
        pause_response = client.post(
            f"/api/v1/goals/{self.goal.id}/pause",
            json={"reason": "operator requested pause"},
            headers=self._headers(self.owner),
        )
        self.assertEqual(pause_response.status_code, 201, pause_response.text)
        pause_body = pause_response.json()
        self.assertEqual(pause_body["status"], "applied")
        self.assertEqual(pause_body["command_type"], "pause")
        self.assertEqual(pause_body["prior_goal_status"], "active")
        self.assertEqual(pause_body["target_goal_status"], "paused")

        goal_after_pause = client.get(f"/api/v1/goals/{self.goal.id}", headers=self._headers(self.owner)).json()
        self.assertEqual(goal_after_pause["status"], "paused")
        self.assertEqual(goal_after_pause["pending_control"], "pause")
        self.assertEqual(goal_after_pause["control_version"], 1)

        resume_response = client.post(
            f"/api/v1/goals/{self.goal.id}/resume", json={}, headers=self._headers(self.owner)
        )
        self.assertEqual(resume_response.status_code, 201, resume_response.text)
        self.assertEqual(resume_response.json()["target_goal_status"], "active")

        cancel_response = client.post(
            f"/api/v1/goals/{self.goal.id}/cancel", json={}, headers=self._headers(self.owner)
        )
        self.assertEqual(cancel_response.status_code, 201, cancel_response.text)
        cancel_body = cancel_response.json()
        self.assertEqual(cancel_body["target_goal_status"], "cancelled")
        self.assertIsNotNone(cancel_body["cancellation_grace_expires_at"])

        goal_after_cancel = client.get(f"/api/v1/goals/{self.goal.id}", headers=self._headers(self.owner)).json()
        self.assertEqual(goal_after_cancel["status"], "cancelled")
        self.assertIsNotNone(goal_after_cancel["cancellation_grace_expires_at"])
        self.assertEqual(goal_after_cancel["control_version"], 3)

    def test_invalid_transition_is_rejected_and_persisted_for_audit(self) -> None:
        response = client.post(
            f"/api/v1/goals/{self.goal.id}/resume", json={}, headers=self._headers(self.owner)
        )
        self.assertEqual(response.status_code, 409)

        commands = client.get(
            f"/api/v1/goals/{self.goal.id}/lifecycle-commands", headers=self._headers(self.owner)
        ).json()
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["status"], "rejected")
        self.assertEqual(commands[0]["command_type"], "resume")

        goal = client.get(f"/api/v1/goals/{self.goal.id}", headers=self._headers(self.owner)).json()
        self.assertEqual(goal["status"], "active")

    def test_idempotency_key_replay_does_not_duplicate_command(self) -> None:
        idempotency_key = f"pause-{uuid.uuid4()}"
        first = client.post(
            f"/api/v1/goals/{self.goal.id}/pause",
            json={"idempotency_key": idempotency_key},
            headers=self._headers(self.owner),
        )
        second = client.post(
            f"/api/v1/goals/{self.goal.id}/pause",
            json={"idempotency_key": idempotency_key},
            headers=self._headers(self.owner),
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json()["id"], second.json()["id"])

        commands = client.get(
            f"/api/v1/goals/{self.goal.id}/lifecycle-commands", headers=self._headers(self.owner)
        ).json()
        self.assertEqual(len(commands), 1)

    def test_steering_request_created_with_default_base_revision(self) -> None:
        response = client.post(
            f"/api/v1/goals/{self.goal.id}/steer",
            json={"instruction": "Add a review task before completion"},
            headers=self._headers(self.owner),
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["status"], "requested")
        self.assertEqual(body["base_revision_number"], self.goal.active_graph_revision_number)
        self.assertIsNone(body["applied_revision_number"])

        listed = client.get(
            f"/api/v1/goals/{self.goal.id}/steering-requests", headers=self._headers(self.owner)
        ).json()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], body["id"])

    def test_steering_rejected_for_terminal_goal(self) -> None:
        with SessionLocal.begin() as session:
            completed_goal = make_goal(session, self.project, self.owner, title="Done", status="completed")
            session.expunge(completed_goal)

        response = client.post(
            f"/api/v1/goals/{completed_goal.id}/steer",
            json={"instruction": "Try to revise a finished goal"},
            headers=self._headers(self.owner),
        )
        self.assertEqual(response.status_code, 409)

    def test_steering_rejects_blank_instruction(self) -> None:
        response = client.post(
            f"/api/v1/goals/{self.goal.id}/steer",
            json={"instruction": "   "},
            headers=self._headers(self.owner),
        )
        self.assertEqual(response.status_code, 422)

    def test_lifecycle_events_are_ordered_and_attributed(self) -> None:
        client.post(f"/api/v1/goals/{self.goal.id}/pause", json={}, headers=self._headers(self.owner))
        client.post(f"/api/v1/goals/{self.goal.id}/resume", json={}, headers=self._headers(self.owner))

        events = client.get(
            f"/api/v1/goals/{self.goal.id}/lifecycle-events", headers=self._headers(self.owner)
        ).json()
        self.assertEqual([event["event_type"] for event in events], ["goal.pause.applied", "goal.resume.applied"])
        self.assertTrue(events[0]["sequence_number"] < events[1]["sequence_number"])
        self.assertEqual(events[0]["actor_id"], str(self.owner.id))

        with SessionLocal() as session:
            persisted = list(
                session.execute(
                    select(GoalLifecycleEvent).where(GoalLifecycleEvent.goal_id == self.goal.id)
                ).scalars()
            )
            self.assertEqual(len(persisted), 2)

    def test_graph_revision_listing_and_detail(self) -> None:
        with SessionLocal.begin() as session:
            revision = make_task_graph_revision(session, self.goal, self.owner, revision_number=1)
            task = Task(goal_id=self.goal.id, created_by=self.owner.id, title="New task")
            session.add(task)
            session.flush()
            session.add(
                TaskGraphRevisionTask(
                    revision_id=revision.id,
                    task_id=task.id,
                    change_type="added",
                    task_snapshot={"title": "New task"},
                )
            )
            session.flush()
            session.expunge(revision)

        listed = client.get(
            f"/api/v1/goals/{self.goal.id}/graph-revisions", headers=self._headers(self.owner)
        ).json()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["revision_number"], 1)

        detail = client.get(
            f"/api/v1/goals/{self.goal.id}/graph-revisions/1", headers=self._headers(self.owner)
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        detail_body = detail.json()
        self.assertEqual(len(detail_body["tasks"]), 1)
        self.assertEqual(detail_body["tasks"][0]["change_type"], "added")

        missing = client.get(
            f"/api/v1/goals/{self.goal.id}/graph-revisions/999", headers=self._headers(self.owner)
        )
        self.assertEqual(missing.status_code, 404)

    def test_apply_steering_adds_and_revises_tasks_as_a_durable_revision(self) -> None:
        with SessionLocal.begin() as session:
            research = Task(
                goal_id=self.goal.id,
                created_by=self.owner.id,
                title="Research",
                status="completed",
                policy_ids=[],
            )
            draft = Task(
                goal_id=self.goal.id,
                created_by=self.owner.id,
                title="Draft",
                status="ready",
                required_capabilities={"writing": True},
                policy_ids=[],
                assignment_status="assigned",
                assignment_rationale={"reason": "best writer"},
            )
            publish = Task(
                goal_id=self.goal.id,
                created_by=self.owner.id,
                title="Publish",
                status="blocked",
                policy_ids=[],
            )
            session.add_all([research, draft, publish])
            session.flush()
            session.add_all(
                [
                    TaskDependency(task_id=draft.id, depends_on_task_id=research.id),
                    TaskDependency(task_id=publish.id, depends_on_task_id=draft.id),
                ]
            )
            session.flush()
            research_id = research.id
            draft_id = draft.id
            publish_id = publish.id

        steer = client.post(
            f"/api/v1/goals/{self.goal.id}/steer",
            json={"instruction": "Revise the draft and add a review before publishing"},
            headers=self._headers(self.owner),
        )
        self.assertEqual(steer.status_code, 201, steer.text)
        request_id = steer.json()["id"]
        apply = client.post(
            f"/api/v1/goals/{self.goal.id}/steering-requests/{request_id}/apply",
            json={
                "change_summary": "Revise draft and insert review",
                "changes": [
                    {
                        "change_type": "revised",
                        "task_id": str(draft_id),
                        "task": {
                            "client_id": "revised-draft",
                            "title": "Revised draft",
                            "required_capabilities": {"writing": True},
                        },
                    },
                    {
                        "change_type": "added",
                        "task": {
                            "client_id": "review",
                            "title": "Review revised draft",
                            "depends_on": ["revised-draft"],
                        },
                    },
                ],
            },
            headers=self._headers(self.owner),
        )
        self.assertEqual(apply.status_code, 201, apply.text)
        body = apply.json()
        self.assertEqual(body["revision_number"], 1)
        self.assertEqual(body["parent_revision_number"], 0)
        self.assertEqual(body["created_by"], str(self.owner.id))
        self.assertEqual(
            {entry["change_type"] for entry in body["tasks"]},
            {"added", "revised", "superseded"},
        )

        with SessionLocal() as session:
            request = session.get(GoalSteeringRequest, uuid.UUID(request_id))
            revision = session.execute(
                select(TaskGraphRevision).where(
                    TaskGraphRevision.steering_request_id == request.id
                )
            ).scalar_one()
            goal = session.get(type(self.goal), self.goal.id)
            old_draft = session.get(Task, draft_id)
            revised_draft = session.execute(
                select(Task).where(
                    Task.goal_id == self.goal.id,
                    Task.title == "Revised draft",
                )
            ).scalar_one()
            review = session.execute(
                select(Task).where(
                    Task.goal_id == self.goal.id,
                    Task.title == "Review revised draft",
                )
            ).scalar_one()
            dependencies = {
                (row.task_id, row.depends_on_task_id)
                for row in session.execute(select(TaskDependency)).scalars()
            }

            self.assertEqual(request.status, "applied")
            self.assertEqual(request.applied_revision_number, 1)
            self.assertEqual(goal.active_graph_revision_number, 1)
            self.assertEqual(old_draft.status, "cancelled")
            self.assertEqual(revised_draft.assignment_status, "unassigned")
            self.assertEqual(
                revision.assignment_evidence[str(draft_id)]["assignment_rationale"],
                {"reason": "best writer"},
            )
            self.assertFalse(
                revision.assignment_evidence[str(draft_id)]["effective"]
            )
            self.assertEqual(
                revision.assignment_evidence[str(draft_id)]["replacement_task_id"],
                str(revised_draft.id),
            )
            self.assertIn((revised_draft.id, research_id), dependencies)
            self.assertIn((review.id, revised_draft.id), dependencies)
            self.assertIn((publish_id, revised_draft.id), dependencies)
            self.assertNotIn((publish_id, draft_id), dependencies)
            self.assertIn(str(revised_draft.id), revision.assignment_evidence)
            self.assertIn(str(review.id), revision.policy_context)
            self.assertIn(str(review.id), revision.budget_context)

        replay = client.post(
            f"/api/v1/goals/{self.goal.id}/steering-requests/{request_id}/apply",
            json={
                "change_summary": "Ignored replay payload",
                "changes": [
                    {
                        "change_type": "added",
                        "task": {"client_id": "ignored", "title": "Ignored"},
                    }
                ],
            },
            headers=self._headers(self.owner),
        )
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json()["id"], body["id"])

    def test_apply_steering_rejects_completed_task_mutation(self) -> None:
        with SessionLocal.begin() as session:
            completed = Task(
                goal_id=self.goal.id,
                created_by=self.owner.id,
                title="Immutable completed work",
                status="completed",
            )
            session.add(completed)
            session.flush()
            completed_id = completed.id

        steer = client.post(
            f"/api/v1/goals/{self.goal.id}/steer",
            json={"instruction": "Replace completed work"},
            headers=self._headers(self.owner),
        )
        response = client.post(
            (
                f"/api/v1/goals/{self.goal.id}/steering-requests/"
                f"{steer.json()['id']}/apply"
            ),
            json={
                "change_summary": "Attempt unsafe rewrite",
                "changes": [
                    {
                        "change_type": "superseded",
                        "task_id": str(completed_id),
                    }
                ],
            },
            headers=self._headers(self.owner),
        )
        self.assertEqual(response.status_code, 409, response.text)
        with SessionLocal() as session:
            self.assertEqual(session.get(Task, completed_id).status, "completed")
            request = session.get(GoalSteeringRequest, uuid.UUID(steer.json()["id"]))
            self.assertEqual(request.status, "requested")

    def test_apply_steering_rejects_stale_base_revision(self) -> None:
        steer = client.post(
            f"/api/v1/goals/{self.goal.id}/steer",
            json={
                "instruction": "Apply against revision zero",
                "base_revision_number": 0,
            },
            headers=self._headers(self.owner),
        )
        with SessionLocal.begin() as session:
            goal = session.get(type(self.goal), self.goal.id)
            goal.active_graph_revision_number = 1

        response = client.post(
            (
                f"/api/v1/goals/{self.goal.id}/steering-requests/"
                f"{steer.json()['id']}/apply"
            ),
            json={
                "change_summary": "Stale addition",
                "changes": [
                    {
                        "change_type": "added",
                        "task": {"client_id": "late", "title": "Late task"},
                    }
                ],
            },
            headers=self._headers(self.owner),
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("base revision is stale", response.json()["detail"])

    def test_lifecycle_and_steering_evidence_survives_api_restart(self) -> None:
        with SessionLocal.begin() as session:
            completed = Task(
                goal_id=self.goal.id,
                created_by=self.owner.id,
                title="Preserve completed evidence",
                status="completed",
            )
            remaining = Task(
                goal_id=self.goal.id,
                created_by=self.owner.id,
                title="Revise remaining work",
                status="ready",
            )
            session.add_all([completed, remaining])
            session.flush()
            completed_id = completed.id
            remaining_id = remaining.id

        pause = client.post(
            f"/api/v1/goals/{self.goal.id}/pause",
            json={"reason": "verify restart boundary"},
            headers=self._headers(self.owner),
        )
        self.assertEqual(pause.status_code, 201, pause.text)

        steer = client.post(
            f"/api/v1/goals/{self.goal.id}/steer",
            json={"instruction": "Replace remaining work with a reviewed version"},
            headers=self._headers(self.owner),
        )
        self.assertEqual(steer.status_code, 201, steer.text)
        apply = client.post(
            (
                f"/api/v1/goals/{self.goal.id}/steering-requests/"
                f"{steer.json()['id']}/apply"
            ),
            json={
                "change_summary": "Replace unfinished work after operator steering",
                "changes": [
                    {
                        "change_type": "revised",
                        "task_id": str(remaining_id),
                        "task": {
                            "client_id": "reviewed-work",
                            "title": "Reviewed remaining work",
                        },
                    }
                ],
            },
            headers=self._headers(self.owner),
        )
        self.assertEqual(apply.status_code, 201, apply.text)

        resume = client.post(
            f"/api/v1/goals/{self.goal.id}/resume",
            json={},
            headers=self._headers(self.owner),
        )
        self.assertEqual(resume.status_code, 201, resume.text)
        cancel = client.post(
            f"/api/v1/goals/{self.goal.id}/cancel",
            json={"reason": "verify durable cancellation"},
            headers=self._headers(self.owner),
        )
        self.assertEqual(cancel.status_code, 201, cancel.text)

        # Clearing the process-wide engine cache and constructing a new app
        # exercises the same committed PostgreSQL boundary used after an API
        # process restart.
        from agentic_os.api.app import create_app
        from agentic_os.api.deps import _engine
        from fastapi.testclient import TestClient

        if _engine.cache_info().currsize:
            _engine().dispose()
            _engine.cache_clear()

        with TestClient(create_app()) as restarted_client:
            headers = self._headers(self.owner)
            goal = restarted_client.get(
                f"/api/v1/goals/{self.goal.id}", headers=headers
            )
            commands = restarted_client.get(
                f"/api/v1/goals/{self.goal.id}/lifecycle-commands",
                headers=headers,
            )
            steering = restarted_client.get(
                f"/api/v1/goals/{self.goal.id}/steering-requests",
                headers=headers,
            )
            revisions = restarted_client.get(
                f"/api/v1/goals/{self.goal.id}/graph-revisions",
                headers=headers,
            )
            events = restarted_client.get(
                f"/api/v1/goals/{self.goal.id}/lifecycle-events",
                headers=headers,
            )

        for response in (goal, commands, steering, revisions, events):
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(goal.json()["status"], "cancelled")
        self.assertEqual(goal.json()["control_version"], 3)
        self.assertEqual(
            [record["command_type"] for record in commands.json()],
            ["pause", "resume", "cancel"],
        )
        self.assertEqual(steering.json()[0]["status"], "applied")
        self.assertEqual(steering.json()[0]["applied_revision_number"], 1)
        self.assertEqual(revisions.json()[0]["revision_number"], 1)

        persisted_events = events.json()
        self.assertEqual(
            [event["event_type"] for event in persisted_events],
            [
                "goal.pause.applied",
                "goal.steering.requested",
                "goal.steering.applied",
                "goal.resume.applied",
                "goal.cancel.applied",
            ],
        )
        self.assertEqual(
            [event["sequence_number"] for event in persisted_events],
            sorted(event["sequence_number"] for event in persisted_events),
        )
        self.assertTrue(
            all(event["actor_id"] == str(self.owner.id) for event in persisted_events)
        )

        with SessionLocal() as session:
            self.assertEqual(session.get(Task, completed_id).status, "completed")
            self.assertEqual(session.get(Task, remaining_id).status, "cancelled")
            replacement = session.execute(
                select(Task).where(
                    Task.goal_id == self.goal.id,
                    Task.title == "Reviewed remaining work",
                )
            ).scalar_one()
            self.assertEqual(replacement.status, "pending")

    def test_cross_team_and_admin_access(self) -> None:
        outsider_response = client.post(
            f"/api/v1/goals/{self.goal.id}/pause", json={}, headers=self._headers(self.outsider)
        )
        self.assertEqual(outsider_response.status_code, 404)

        outsider_list = client.get(
            f"/api/v1/goals/{self.goal.id}/lifecycle-commands", headers=self._headers(self.outsider)
        )
        self.assertEqual(outsider_list.status_code, 404)

        admin_response = client.post(
            f"/api/v1/goals/{self.goal.id}/pause", json={}, headers=self._headers(self.admin)
        )
        self.assertEqual(admin_response.status_code, 201, admin_response.text)
