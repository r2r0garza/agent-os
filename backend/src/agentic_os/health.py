"""Deployment dependency health evidence.

Aggregates the startup/runtime dependency checks a local deployment needs to
trust before serving traffic or claiming tasks: PostgreSQL reachability,
applied migrations, artifact-root writability, master-key availability, and
(for worker-role callers) sandbox runtime availability. Every check fails
closed - an unreachable or ambiguous dependency is reported unhealthy rather
than skipped.
"""
from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from agentic_os.config import validate_artifact_root, validate_master_key
from agentic_os.sandbox import runtime_available


@dataclass(frozen=True)
class DependencyStatus:
    name: str
    status: str  # "healthy" or "unavailable"
    detail: str


def check_database(engine: Engine) -> DependencyStatus:
    started = perf_counter()
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as error:
        return DependencyStatus("database", "unavailable", f"database is unreachable: {error}")
    latency_ms = round((perf_counter() - started) * 1000, 3)
    return DependencyStatus("database", "healthy", f"connected in {latency_ms}ms")


def check_migrations(engine: Engine) -> DependencyStatus:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from agentic_os.operations import OperationError, _alembic_config

    try:
        config_path = _alembic_config()
    except OperationError as error:
        return DependencyStatus("migrations", "unavailable", str(error))
    heads = set(ScriptDirectory.from_config(Config(str(config_path))).get_heads())
    try:
        with engine.connect() as connection:
            current = {
                row[0] for row in connection.execute(text("SELECT version_num FROM alembic_version"))
            }
    except ProgrammingError:
        return DependencyStatus(
            "migrations",
            "unavailable",
            "no migration has been applied; run 'agentic-os operations migrations apply'",
        )
    except SQLAlchemyError as error:
        return DependencyStatus(
            "migrations",
            "unavailable",
            f"cannot read migration state: {error}",
        )
    if not current:
        return DependencyStatus(
            "migrations",
            "unavailable",
            "no migration has been applied; run 'agentic-os operations migrations apply'",
        )
    if current != heads:
        return DependencyStatus(
            "migrations",
            "unavailable",
            f"database is at revision(s) {sorted(current)} but code expects head "
            f"{sorted(heads)}; run 'agentic-os operations migrations apply'",
        )
    return DependencyStatus("migrations", "healthy", f"database is at head revision(s) {sorted(heads)}")


def check_artifact_root() -> DependencyStatus:
    result = validate_artifact_root()
    return DependencyStatus("artifact_root", "healthy" if result.ok else "unavailable", result.detail)


def check_master_key(*, required: bool = True) -> DependencyStatus:
    result = validate_master_key(required=required)
    return DependencyStatus("master_key", "healthy" if result.ok else "unavailable", result.detail)


def check_sandbox() -> DependencyStatus:
    reasons = []
    any_available = False
    for runtime in ("docker", "podman"):
        available, reason = runtime_available(runtime)
        any_available = any_available or available
        reasons.append(f"{runtime}: available" if available else f"{runtime}: unavailable ({reason})")
    status = "healthy" if any_available else "unavailable"
    return DependencyStatus("sandbox", status, "; ".join(reasons))


def deployment_health(
    engine: Engine, *, include_sandbox: bool = False, require_master_key: bool = True
) -> dict:
    """Aggregate dependency evidence, failing closed on any unhealthy check."""
    checks = [
        check_database(engine),
        check_migrations(engine),
        check_artifact_root(),
        check_master_key(required=require_master_key),
    ]
    if include_sandbox:
        checks.append(check_sandbox())
    overall = "healthy" if all(check.status == "healthy" for check in checks) else "unavailable"
    return {
        "status": overall,
        "checks": {check.name: {"status": check.status, "detail": check.detail} for check in checks},
    }
