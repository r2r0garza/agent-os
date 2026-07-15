from __future__ import annotations

import argparse
import json
import sys
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

    worker = commands.add_parser("worker", help="run the durable task worker")
    worker_actions = worker.add_subparsers(dest="worker_command", required=True)
    run_once_parser = worker_actions.add_parser(
        "run-once", help="claim and execute ready tasks until none remain"
    )
    run_once_parser.add_argument("--worker-id", default=None, help="defaults to a random worker id")
    return root


def _run_worker_run_once(worker_id: str | None) -> int:
    from agentic_os.domain import create_database_engine, session_factory
    from agentic_os.worker import run_task_worker_once

    resolved_worker_id = worker_id or f"cli-{uuid.uuid4()}"
    engine = create_database_engine()
    session_maker = session_factory(engine)
    claimed = 0
    try:
        while True:
            with session_maker() as session:
                try:
                    task = run_task_worker_once(session, resolved_worker_id)
                except Exception as error:
                    session.commit()
                    print(f"task execution failed: {error}", file=sys.stderr)
                    return 1
                session.commit()
                if task is None:
                    break
                claimed += 1
                print(json.dumps({"task_id": str(task.id), "status": task.status}, sort_keys=True))
    finally:
        engine.dispose()
    print(f"claimed and processed {claimed} task(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "worker":
        return _run_worker_run_once(args.worker_id)
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

