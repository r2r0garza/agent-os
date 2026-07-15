from __future__ import annotations

import argparse
import json
import sys
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
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
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

