"""Regression coverage for the default-operator bootstrap race.

An empty, freshly migrated database with no `X-Agentic-User-Id` header used
to let concurrent cold-start requests race past the check-then-insert logic
in `agentic_os.api.bootstrap`, producing unhandled `IntegrityError`/500s or
(for the team row, which had no uniqueness constraint at all) silently
duplicated default rows. These tests prove concurrent callers all resolve to
exactly one default team, user, and membership.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from sqlalchemy import create_engine, select

from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import Team, TeamMembership, User

BACKEND_ROOT = Path(__file__).parents[1]

INVENTORY_ENDPOINTS = (
    "/api/v1/model-profiles",
    "/api/v1/projects",
    "/api/v1/agents",
    "/api/v1/skills",
    "/api/v1/mcp-servers",
)

CONCURRENCY = 8


def _reset_schema(db_url: str) -> None:
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


def setUpModule() -> None:
    global TEST_DATABASE_URL, SessionLocal
    TEST_DATABASE_URL = os.environ.get("AGENTIC_OS_DATABASE_URL", database_url())
    try:
        probe = create_database_engine(TEST_DATABASE_URL)
        with probe.connect():
            pass
        probe.dispose()
    except Exception as error:  # pragma: no cover - environment guard
        raise unittest.SkipTest(
            f"PostgreSQL is not reachable at {TEST_DATABASE_URL!r}; set "
            "AGENTIC_OS_DATABASE_URL to run bootstrap concurrency tests: "
            f"{error}"
        )
    os.environ["AGENTIC_OS_DATABASE_URL"] = TEST_DATABASE_URL
    SessionLocal = session_factory(create_database_engine(TEST_DATABASE_URL))


class ConcurrentHelperBootstrapTests(unittest.TestCase):
    """Direct concurrent calls to the bootstrap helpers themselves."""

    def setUp(self) -> None:
        _reset_schema(TEST_DATABASE_URL)

    def test_concurrent_ensure_default_team_resolves_to_one_row(self) -> None:
        from agentic_os.api.bootstrap import ensure_default_team

        def _call() -> str:
            with SessionLocal() as session:
                team = ensure_default_team(session)
                session.commit()
                return str(team.id)

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
            team_ids = list(executor.map(lambda _: _call(), range(CONCURRENCY)))

        self.assertEqual(len(set(team_ids)), 1)
        with SessionLocal() as session:
            count = session.execute(
                select(Team).where(Team.name == "Default Team")
            ).scalars().all()
            self.assertEqual(len(count), 1)

    def test_concurrent_ensure_default_user_resolves_to_one_row(self) -> None:
        from agentic_os.api.bootstrap import ensure_default_user

        def _call() -> str:
            with SessionLocal() as session:
                user = ensure_default_user(session)
                session.commit()
                return str(user.id)

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
            user_ids = list(executor.map(lambda _: _call(), range(CONCURRENCY)))

        self.assertEqual(len(set(user_ids)), 1)
        with SessionLocal() as session:
            rows = session.execute(
                select(User).where(User.email == "operator@local")
            ).scalars().all()
            self.assertEqual(len(rows), 1)

    def test_concurrent_ensure_default_team_membership_resolves_to_one_row(self) -> None:
        from agentic_os.api.bootstrap import ensure_default_team_membership

        def _call() -> tuple[str, str]:
            with SessionLocal() as session:
                team, user = ensure_default_team_membership(session)
                session.commit()
                return str(team.id), str(user.id)

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
            results = list(executor.map(lambda _: _call(), range(CONCURRENCY)))

        self.assertEqual(len(set(results)), 1)
        team_id, user_id = results[0]
        with SessionLocal() as session:
            memberships = session.execute(
                select(TeamMembership).where(
                    TeamMembership.team_id == team_id, TeamMembership.user_id == user_id
                )
            ).scalars().all()
            self.assertEqual(len(memberships), 1)
            self.assertEqual(memberships[0].role, "owner")


class ConcurrentInventoryFanoutTests(unittest.TestCase):
    """API-level reproduction: the frontend's concurrent inventory fan-out
    against an empty database with no identity header."""

    def setUp(self) -> None:
        _reset_schema(TEST_DATABASE_URL)
        from agentic_os.api.deps import _engine

        if _engine.cache_info().currsize:
            _engine().dispose()
            _engine.cache_clear()
        from fastapi.testclient import TestClient

        from agentic_os.api.app import create_app

        self.client = TestClient(create_app())

    def test_concurrent_cold_start_inventory_requests_all_succeed(self) -> None:
        endpoints = list(INVENTORY_ENDPOINTS) * 2

        with ThreadPoolExecutor(max_workers=len(endpoints)) as executor:
            responses = list(executor.map(self.client.get, endpoints))

        for endpoint, response in zip(endpoints, responses):
            self.assertEqual(
                response.status_code, 200, f"{endpoint} -> {response.status_code}: {response.text}"
            )

        with SessionLocal() as session:
            self.assertEqual(
                len(session.execute(select(Team).where(Team.name == "Default Team")).scalars().all()),
                1,
            )
            self.assertEqual(
                len(session.execute(select(User).where(User.email == "operator@local")).scalars().all()),
                1,
            )
            self.assertEqual(len(session.execute(select(TeamMembership)).scalars().all()), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
