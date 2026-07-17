"""Bounded local deployment operations with inspectable evidence.

Database credentials and master-key material are deliberately excluded from
backup bundles and PostgreSQL child-process arguments. Operators must back up
and restore the master key through their own encrypted secret-management
channel.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError

from agentic_os.config import (
    ARTIFACT_ROOT_ENV,
    BACKUP_ROOT_ENV,
    MASTER_KEY_ENV,
    MASTER_KEY_FILE_ENV,
    TEAM_DEPLOYMENT_MODE,
    TELEMETRY_DISABLED_ENV,
    format_report,
    resolve_deployment_mode,
    run_preflight,
    validate_postgres_tools,
)
from agentic_os.domain.database import create_database_engine, database_url, session_factory

BACKUP_FORMAT_VERSION = 1


class OperationError(RuntimeError):
    """Raised when an operator command cannot proceed safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _alembic_config() -> Path:
    candidates = (
        Path.cwd() / "alembic.ini",
        Path.cwd() / "backend" / "alembic.ini",
        Path(__file__).resolve().parents[2] / "alembic.ini",
        Path("/app/alembic.ini"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    checked = ", ".join(str(path) for path in candidates)
    raise OperationError(f"Alembic configuration not found; checked: {checked}")


def _run(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, env=env, capture_output=True, text=True, check=True)
    except FileNotFoundError as error:
        raise OperationError(f"required executable is unavailable: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "command failed").strip()
        raise OperationError(f"{command[0]} failed: {detail}") from error


def _postgres_environment(url_value: str) -> dict[str, str]:
    try:
        parsed: URL = make_url(url_value)
    except Exception as error:
        raise OperationError("database URL is malformed") from error
    if not parsed.drivername.startswith("postgresql") or not parsed.host or not parsed.database:
        raise OperationError("database URL must identify a PostgreSQL host and database")
    environment = dict(os.environ)
    environment.update(
        {
            "PGHOST": parsed.host,
            "PGPORT": str(parsed.port or 5432),
            "PGDATABASE": parsed.database,
        }
    )
    if parsed.username:
        environment["PGUSER"] = parsed.username
    if parsed.password:
        environment["PGPASSWORD"] = parsed.password
    return environment


def _sanitized_configuration(url_value: str, artifact_root: Path) -> dict[str, Any]:
    parsed = make_url(url_value)
    if os.environ.get(MASTER_KEY_ENV):
        key_source = "environment"
    elif os.environ.get(MASTER_KEY_FILE_ENV):
        key_source = "file"
    else:
        key_source = "default-file-or-unset"
    return {
        "database": {
            "driver": parsed.drivername,
            "host": parsed.host,
            "port": parsed.port or 5432,
            "database": parsed.database,
            "username": parsed.username,
        },
        "artifact_root": str(artifact_root),
        "master_key_source": key_source,
        "master_key_included": False,
        "telemetry_disabled": os.environ.get(TELEMETRY_DISABLED_ENV, "true"),
    }


def _record_maintenance_event(event_type: str, payload: dict[str, Any]) -> None:
    """Best-effort durable evidence for an operator maintenance action.

    Maintenance commands must still succeed when the audit trail's own
    database is unreachable (for example, a preflight check run before the
    database exists), so persistence failures are swallowed rather than
    surfaced as command failures.
    """
    from agentic_os.domain.models import AuditEvent

    try:
        engine = create_database_engine()
        try:
            with session_factory(engine)() as session:
                session.add(AuditEvent(event_type=event_type, payload=payload))
                session.commit()
        finally:
            engine.dispose()
    except SQLAlchemyError:
        pass


def setup_check() -> str:
    results = run_preflight()
    if not any(result.name == "postgres_tools" for result in results):
        results = results + [validate_postgres_tools()]
    report = format_report(results)
    if not all(result.ok for result in results):
        _record_maintenance_event("operations.setup_check", {"ok": False, "report": report.splitlines()})
        raise OperationError(report)
    try:
        _run(["pg_isready", "--quiet"], env=_postgres_environment(database_url()))
    except OperationError as error:
        report += f"\n[FAIL] database_connection: {error}"
        _record_maintenance_event("operations.setup_check", {"ok": False, "report": report.splitlines()})
        raise OperationError(report) from error
    report += "\n[OK] database_connection: PostgreSQL accepts connections"
    _record_maintenance_event("operations.setup_check", {"ok": True, "report": report.splitlines()})
    return report


def migration_status() -> str:
    config = _alembic_config()
    result = _run(["alembic", "-c", str(config), "current", "--check-heads"])
    detail = result.stdout.strip() or "database is at the migration head"
    _record_maintenance_event("operations.migration_status", {"detail": detail})
    return detail


def apply_migrations() -> str:
    config = _alembic_config()
    result = _run(["alembic", "-c", str(config), "upgrade", "head"])
    detail = result.stdout.strip() or "migrations applied through head"
    _record_maintenance_event("operations.migration_apply", {"detail": detail})
    return detail


def _collect_artifacts(source: Path, destination: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not source.exists():
        source.mkdir(parents=True)
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise OperationError(f"artifact backup refuses symbolic link: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        entries.append(
            {
                "path": relative.as_posix(),
                "size": target.stat().st_size,
                "sha256": _sha256(target),
            }
        )
    return entries


def _resolve_backup_target(output: str | Path | None) -> Path:
    if output is not None:
        return Path(output).resolve()
    backup_root = os.environ.get(BACKUP_ROOT_ENV)
    if not backup_root:
        raise OperationError(
            f"backup output not provided; pass --output or set {BACKUP_ROOT_ENV} to a durable "
            "local/mounted backup directory"
        )
    if "://" in backup_root:
        raise OperationError(
            f"{BACKUP_ROOT_ENV} must be a local/mounted durable filesystem path, not a remote URI "
            f"(got {backup_root!r})"
        )
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return (Path(backup_root) / f"agentic-os-{timestamp}.tar.gz").resolve()


def create_backup(output: str | Path | None = None) -> dict[str, Any]:
    target = _resolve_backup_target(output)
    if target.exists():
        raise OperationError(f"backup target already exists: {target}")
    artifact_root = Path(
        os.environ.get(
            ARTIFACT_ROOT_ENV,
            Path.home() / ".local/share/agentic-os/artifacts",
        )
    )
    try:
        target.relative_to(artifact_root.resolve())
    except ValueError:
        pass
    else:
        raise OperationError("backup output must be outside the artifact root")
    url_value = database_url()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
        staging = Path(temporary) / "agentic-os-backup"
        artifacts = staging / "artifacts"
        artifacts.mkdir(parents=True)
        database_dump = staging / "database.dump"
        _run(
            ["pg_dump", "--format=custom", "--file", str(database_dump)],
            env=_postgres_environment(url_value),
        )
        artifact_entries = _collect_artifacts(artifact_root, artifacts)
        configuration = _sanitized_configuration(url_value, artifact_root)
        (staging / "configuration.json").write_text(
            json.dumps(configuration, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        manifest = {
            "format_version": BACKUP_FORMAT_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "database": {"path": "database.dump", "sha256": _sha256(database_dump)},
            "artifacts": artifact_entries,
            "configuration": {
                "path": "configuration.json",
                "sha256": _sha256(staging / "configuration.json"),
            },
            "master_key": {
                "included": False,
                "instruction": "Restore matching master-key material separately before startup.",
            },
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        with tarfile.open(target, "x:gz") as archive:
            archive.add(staging, arcname="agentic-os-backup", recursive=True)
    _record_maintenance_event("operations.backup_created", {"backup": str(target), "manifest": manifest})
    return {"backup": str(target), "artifact_files": len(artifact_entries), "manifest": manifest}


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
            raise OperationError(f"unsafe path in backup archive: {member.name}")
    archive.extractall(destination, filter="data")


def verify_backup(backup: str | Path, destination: Path) -> tuple[Path, dict[str, Any]]:
    source = Path(backup).resolve()
    if not source.is_file():
        raise OperationError(f"backup archive does not exist: {source}")
    try:
        with tarfile.open(source, "r:gz") as archive:
            _safe_extract(archive, destination)
    except (tarfile.TarError, OSError) as error:
        raise OperationError(f"cannot read backup archive: {error}") from error
    root = destination / "agentic-os-backup"
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OperationError("backup manifest is missing or invalid") from error
    if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        raise OperationError(f"unsupported backup format version: {manifest.get('format_version')!r}")
    try:
        checks = [manifest["database"], manifest["configuration"]]
        checks.extend(
            {"path": f"artifacts/{item['path']}", "sha256": item["sha256"]}
            for item in manifest["artifacts"]
        )
    except (KeyError, TypeError) as error:
        raise OperationError("backup manifest does not describe the required payloads") from error
    for item in checks:
        path = root / item["path"]
        if not path.is_file() or _sha256(path) != item["sha256"]:
            raise OperationError(f"backup integrity check failed: {item['path']}")
    return root, manifest


def restore_backup(
    backup: str | Path,
    *,
    target_database_url: str,
    target_artifact_root: str | Path,
    confirm_overwrite: bool = False,
) -> dict[str, Any]:
    active_url = database_url()
    artifact_target = Path(target_artifact_root).resolve()
    active_artifacts = Path(
        os.environ.get(
            ARTIFACT_ROOT_ENV,
            Path.home() / ".local/share/agentic-os/artifacts",
        )
    ).resolve()
    target_database = make_url(target_database_url)
    active_database = make_url(active_url)
    same_database = (
        target_database.host,
        target_database.port or 5432,
        target_database.database,
    ) == (
        active_database.host,
        active_database.port or 5432,
        active_database.database,
    )
    target_has_files = artifact_target.exists() and any(artifact_target.iterdir())
    if not confirm_overwrite and (
        same_database or artifact_target == active_artifacts or target_has_files
    ):
        raise OperationError(
            "restore target is active or non-empty; use isolated database/artifact targets or pass --confirm-overwrite"
        )
    artifact_target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory(
        dir=artifact_target.parent, prefix=".agentic-os-restore-"
    ) as artifact_temporary:
        root, manifest = verify_backup(backup, Path(temporary))
        staged_artifacts = Path(artifact_temporary) / "artifacts"
        shutil.copytree(root / "artifacts", staged_artifacts)
        command = [
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "--dbname",
            target_database.database or "",
        ]
        if confirm_overwrite:
            command.extend(["--clean", "--if-exists"])
        command.append(str(root / "database.dump"))
        _run(command, env=_postgres_environment(target_database_url))
        if artifact_target.exists() and confirm_overwrite:
            shutil.rmtree(artifact_target)
        staged_artifacts.replace(artifact_target)
    result = {
        "backup": str(Path(backup).resolve()),
        "artifact_root": str(artifact_target),
        "artifact_files": len(manifest["artifacts"]),
        "master_key_restored": False,
        "next_step": "Restore matching master-key material separately, then run setup-check and migration status.",
    }
    _record_maintenance_event("operations.restore_completed", result)
    return result


def upgrade_preflight() -> dict[str, Any]:
    report = setup_check()
    migrations = migration_status()
    mode = resolve_deployment_mode()
    rollback = (
        "Create and verify a backup before upgrade; restore database, artifacts, "
        "configuration, and matching master key together if rollback is required."
    )
    if mode == TEAM_DEPLOYMENT_MODE:
        rollback += (
            " On a team VM, also back up and restore the proxy's TLS certificate and "
            "private key separately; the application backup archive does not include them."
        )
    result = {
        "ready": True,
        "deployment_mode": mode,
        "configuration": report.splitlines(),
        "migrations": migrations,
        "rollback": rollback,
    }
    _record_maintenance_event("operations.upgrade_preflight", result)
    return result
