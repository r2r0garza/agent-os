from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from agentic_os.code_index import IndexError, build, check, explain, pre_commit


def _repository(value: str) -> Path:
    return Path(value).resolve()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="agentic-os")
    root.add_argument("--repository", type=_repository, default=Path.cwd())
    commands = root.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="manage the committed code index")
    actions = index.add_subparsers(dest="index_command", required=True)
    build_parser = actions.add_parser("build", help="build the code index")
    build_parser.add_argument("--incremental", action="store_true")
    actions.add_parser("check", help="check committed index freshness")
    actions.add_parser("pre-commit", help="refresh and require generated changes to be staged")
    explain_parser = actions.add_parser("explain", help="explain an indexed symbol")
    explain_parser.add_argument("qualified_name")

    config = commands.add_parser(
        "config", help="validate and manage local configuration and master-key material"
    )
    config_actions = config.add_subparsers(dest="config_command", required=True)
    check_parser = config_actions.add_parser(
        "check", help="validate local or team-deployment configuration and master-key material"
    )
    check_parser.add_argument(
        "--json",
        action="store_true",
        help="print structured evidence instead of the human-readable report",
    )
    generate_key_parser = config_actions.add_parser(
        "generate-master-key", help="generate and persist a local master key"
    )
    generate_key_parser.add_argument(
        "--path",
        default=None,
        help="defaults to AGENTIC_OS_MASTER_KEY_FILE or the standard local configuration path",
    )
    generate_key_parser.add_argument(
        "--force", action="store_true", help="overwrite an existing key file"
    )

    health = commands.add_parser("health", help="check startup and runtime dependency health")
    health_actions = health.add_subparsers(dest="health_command", required=True)
    health_check_parser = health_actions.add_parser(
        "check", help="report database, migration, artifact-root, master-key, and sandbox health"
    )
    health_check_parser.add_argument(
        "--role",
        choices=("api", "worker"),
        default="api",
        help="worker role also checks sandbox runtime availability (default: api)",
    )

    operations = commands.add_parser("operations", help="run bounded local deployment operations")
    operation_actions = operations.add_subparsers(dest="operations_command", required=True)
    operation_actions.add_parser("setup-check", help="validate setup and backup prerequisites")
    migrations = operation_actions.add_parser(
        "migrations", help="inspect or apply database migrations"
    )
    migrations.add_argument("action", choices=("status", "apply"))
    backup = operation_actions.add_parser("backup", help="create an integrity-checked local backup")
    backup.add_argument(
        "--output",
        default=None,
        help="new .tar.gz backup path; defaults to a timestamped file under AGENTIC_OS_BACKUP_ROOT",
    )
    verify = operation_actions.add_parser(
        "verify-backup", help="verify a backup without restoring it"
    )
    verify.add_argument("backup", help="backup .tar.gz path")
    restore = operation_actions.add_parser("restore", help="restore a verified backup")
    restore.add_argument("backup", help="backup .tar.gz path")
    restore.add_argument("--target-database-url", required=True)
    restore.add_argument("--target-artifact-root", required=True)
    restore.add_argument("--confirm-overwrite", action="store_true")
    operation_actions.add_parser(
        "upgrade-preflight", help="check configuration and migrations before upgrade"
    )

    worker = commands.add_parser("worker", help="run the durable task worker")
    worker_actions = worker.add_subparsers(dest="worker_command", required=True)
    run_once_parser = worker_actions.add_parser(
        "run-once", help="claim and execute ready tasks until none remain"
    )
    run_once_parser.add_argument("--worker-id", default=None, help="defaults to a random worker id")
    run_once_parser.add_argument(
        "--lease-seconds", type=int, default=None, help="defaults to the worker's standard lease duration"
    )
    run_once_parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="number of concurrent local worker loops to run (default 1)",
    )
    return root


def _run_worker_run_once(worker_id: str | None, lease_seconds: int | None, workers: int) -> int:
    from agentic_os.domain import create_database_engine, session_factory
    from agentic_os.worker import run_scheduler_once
    from agentic_os.worker.leases import DEFAULT_LEASE_SECONDS

    resolved_worker_id = worker_id or f"cli-{uuid.uuid4()}"
    resolved_lease_seconds = lease_seconds if lease_seconds is not None else DEFAULT_LEASE_SECONDS

    pause_seconds_raw = os.environ.get("AGENTIC_OS_WORKER_TEST_PAUSE_AFTER_RUN_STARTED_SECONDS")
    on_run_started = None
    if pause_seconds_raw:
        pause_seconds = float(pause_seconds_raw)

        def on_run_started() -> None:
            print(f"run started; pausing {pause_seconds}s for restart-recovery verification", file=sys.stderr)
            time.sleep(pause_seconds)

    engine = create_database_engine()
    session_maker = session_factory(engine)
    try:
        result = run_scheduler_once(
            session_maker,
            resolved_worker_id,
            worker_count=workers,
            lease_seconds=resolved_lease_seconds,
            on_run_started=on_run_started,
        )
    finally:
        engine.dispose()

    for task_id, status in result.claimed:
        print(json.dumps({"task_id": task_id, "status": status}, sort_keys=True))
    if result.errors:
        for error in result.errors:
            print(f"task execution failed: {error}", file=sys.stderr)
        return 1
    print(f"claimed and processed {len(result.claimed)} task(s)")
    return 0


def _run_config_command(args: argparse.Namespace) -> int:
    from agentic_os.config import (
        DEFAULT_MASTER_KEY_FILE,
        ConfigurationError,
        format_report,
        generate_master_key,
        preflight_evidence,
        run_preflight,
    )

    if args.config_command == "check":
        results = run_preflight()
        if args.json:
            print(json.dumps(preflight_evidence(results), sort_keys=True))
        else:
            print(format_report(results))
        return 0 if all(result.ok for result in results) else 1
    if args.config_command == "generate-master-key":
        target_path = args.path or os.environ.get("AGENTIC_OS_MASTER_KEY_FILE", DEFAULT_MASTER_KEY_FILE)
        try:
            path = generate_master_key(target_path, force=args.force)
        except ConfigurationError as error:
            print(f"config error: {error}", file=sys.stderr)
            return 2
        print(f"generated master key at {path} (mode 0600); back it up securely and never commit it to source control")
        return 0
    raise AssertionError(f"unknown config command {args.config_command!r}")


def _run_health_command(args: argparse.Namespace) -> int:
    from agentic_os.domain import create_database_engine
    from agentic_os.health import deployment_health

    engine = create_database_engine()
    try:
        report = deployment_health(engine, include_sandbox=args.role == "worker")
    finally:
        engine.dispose()
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "healthy" else 1


def _run_operations_command(args: argparse.Namespace) -> int:
    import tempfile

    from agentic_os.operations import (
        OperationError,
        apply_migrations,
        create_backup,
        migration_status,
        restore_backup,
        setup_check,
        upgrade_preflight,
        verify_backup,
    )

    try:
        if args.operations_command == "setup-check":
            print(setup_check())
        elif args.operations_command == "migrations":
            print(migration_status() if args.action == "status" else apply_migrations())
        elif args.operations_command == "backup":
            print(json.dumps(create_backup(args.output), sort_keys=True))
        elif args.operations_command == "verify-backup":
            with tempfile.TemporaryDirectory() as temporary:
                _, manifest = verify_backup(args.backup, Path(temporary))
            print(json.dumps({"verified": True, "manifest": manifest}, sort_keys=True))
        elif args.operations_command == "restore":
            print(
                json.dumps(
                    restore_backup(
                        args.backup,
                        target_database_url=args.target_database_url,
                        target_artifact_root=args.target_artifact_root,
                        confirm_overwrite=args.confirm_overwrite,
                    ),
                    sort_keys=True,
                )
            )
        elif args.operations_command == "upgrade-preflight":
            print(json.dumps(upgrade_preflight(), sort_keys=True))
        else:
            raise AssertionError(f"unknown operations command {args.operations_command!r}")
    except OperationError as error:
        print(f"operations error: {error}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "worker":
        return _run_worker_run_once(args.worker_id, args.lease_seconds, args.workers)
    if args.command == "config":
        return _run_config_command(args)
    if args.command == "health":
        return _run_health_command(args)
    if args.command == "operations":
        return _run_operations_command(args)
    try:
        if args.index_command == "build":
            result = build(args.repository, incremental=args.incremental)
            print(json.dumps(result, sort_keys=True))
        elif args.index_command == "check":
            stale = check(args.repository)
            if stale:
                print("stale code index: " + ", ".join(stale), file=sys.stderr)
                return 1
            print("code index is current")
        elif args.index_command == "pre-commit":
            unstaged = pre_commit(args.repository)
            if unstaged:
                print("stage refreshed code-index artifacts:", file=sys.stderr)
                for path in unstaged:
                    print(f"  {path}", file=sys.stderr)
                return 1
            print("code index is current and staged")
        else:
            print(json.dumps(explain(args.repository, args.qualified_name), sort_keys=True, indent=2))
    except IndexError as error:
        print(f"code-index error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
