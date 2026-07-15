from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agentic_os.code_index import IndexError, build, check, explain, pre_commit
from agentic_os.code_index.contracts import (
    PARSER_API_VERSION, TrackedFile, canonical_json, jsonl_bytes, stable_id, validate_path,
)
from agentic_os.code_index.discovery import discover
from agentic_os.code_index.python_parser import PythonParser
from agentic_os.code_index.resolver import resolve


class Repository:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=self.path, check=True)
        subprocess.run(["git", "config", "user.email", "index@example.test"], cwd=self.path, check=True)
        subprocess.run(["git", "config", "user.name", "Index Test"], cwd=self.path, check=True)

    def write(self, path: str, content: str | bytes) -> None:
        destination = self.path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content.encode() if isinstance(content, str) else content)

    def commit(self) -> None:
        subprocess.run(["git", "add", "."], cwd=self.path, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=self.path, check=True)

    def close(self) -> None:
        self.temporary.cleanup()


class ContractTests(unittest.TestCase):
    def test_stable_ids_and_canonical_serialization(self) -> None:
        value = stable_id("python", "function", "pkg.work")
        self.assertEqual(value, stable_id("python", "function", "pkg.work"))
        self.assertTrue(value.startswith("python:function:"))
        self.assertEqual(canonical_json({"b": 1, "a": 2}), '{"a":2,"b":1}')
        left = jsonl_bytes([{"z": 1}, {"a": 2}])
        right = jsonl_bytes([{"a": 2}, {"z": 1}])
        self.assertEqual(left, right)
        self.assertTrue(left.endswith(b"\n"))

    def test_repository_path_validation(self) -> None:
        self.assertEqual(validate_path("backend/src/a.py"), "backend/src/a.py")
        for invalid in ("/absolute.py", "../parent.py", "a/../b.py", "a\\b.py"):
            with self.subTest(invalid=invalid), self.assertRaises(IndexError):
                validate_path(invalid)


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Repository()

    def tearDown(self) -> None:
        self.repo.close()

    def test_git_membership_worktree_filters_size_and_order(self) -> None:
        self.repo.write("backend/src/z.py", "old = 1\n")
        self.repo.write("backend/src/a.py", "a = 1\n")
        self.repo.write("frontend/node_modules/ignored.ts", "export {}\n")
        self.repo.write("backend/src/large.py", b"x" * 1_000_001)
        self.repo.commit()
        self.repo.write("backend/src/z.py", "new = 2\n")
        self.repo.write("backend/src/untracked.py", "no = 1\n")
        files = discover(self.repo.path)
        self.assertEqual([item.path for item in files], ["backend/src/a.py", "backend/src/z.py"])
        self.assertEqual(files[1].content, b"new = 2\n")


class PythonParserTests(unittest.TestCase):
    def test_extracts_symbols_metadata_imports_calls_and_spans(self) -> None:
        source = b'''from .helpers import run as execute\n\nclass Worker(Base):\n    @logged\n    def work(self, value: int) -> str:\n        execute(value)\n        self.finish()\n        injected(value)\n        return str(value)\n\n    def finish(self):\n        pass\n\ndef outer():\n    def nested():\n        return execute()\n    return nested()\n'''
        file = TrackedFile("backend/src/pkg/service.py", len(source), "x", source)
        symbols, dependencies = PythonParser().parse(Path.cwd(), file)
        names = {item["qualified_name"]: item for item in symbols}
        self.assertEqual({item["kind"] for item in symbols}, {"module", "class", "method", "function"})
        self.assertIn("pkg.service.outer.nested", names)
        self.assertEqual(names["pkg.service.Worker.work"]["signature"], "self, value: int")
        self.assertEqual(names["pkg.service.Worker"]["extensions"]["python"]["bases"], ["Base"])
        self.assertTrue(all(item["span"]["start"]["line"] >= 1 for item in symbols + dependencies))
        self.assertIn("execute", {item["target"] for item in dependencies})
        self.assertNotIn("str", {item["target"] for item in dependencies})

    def test_syntax_errors_fail_clearly(self) -> None:
        file = TrackedFile("backend/src/bad.py", 4, "x", b"def\n")
        with self.assertRaisesRegex(IndexError, "cannot parse"):
            PythonParser().parse(Path.cwd(), file)

    def test_resolution_is_conservative(self) -> None:
        source = b'''def target():\n    pass\n\nclass Box:\n    def finish(self):\n        pass\n    def work(self):\n        target()\n        self.finish()\n        injected()\n        receiver.finish()\n'''
        file = TrackedFile("backend/src/pkg/mod.py", len(source), "x", source)
        symbols, dependencies = PythonParser().parse(Path.cwd(), file)
        result = resolve(symbols, dependencies)
        calls = {item["target"]: item for item in result if item["kind"] == "call"}
        self.assertEqual(calls["target"]["evidence"], "resolved")
        self.assertEqual(calls["self.finish"]["evidence"], "resolved")
        self.assertEqual(calls["injected"]["evidence"], "unresolved")
        self.assertEqual(calls["receiver.finish"]["evidence"], "unresolved")


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Repository()
        self.repo.write("backend/src/pkg/__init__.py", "")
        self.repo.write("backend/src/pkg/helpers.py", "def run():\n    return 1\n")
        self.repo.write("backend/src/pkg/service.py", "from .helpers import run as execute\n\ndef work():\n    return execute()\n")
        self.repo.commit()

    def tearDown(self) -> None:
        self.repo.close()

    def artifacts(self) -> dict[str, bytes]:
        return {item.name: item.read_bytes() for item in (self.repo.path / ".code-index").iterdir()}

    def test_clean_repeat_incremental_and_explain(self) -> None:
        first = build(self.repo.path)
        first_bytes = self.artifacts()
        second = build(self.repo.path)
        self.assertEqual(first_bytes, self.artifacts())
        incremental = build(self.repo.path, incremental=True)
        self.assertEqual(first_bytes, self.artifacts())
        self.assertEqual(incremental["parsed_files"], 0)
        self.assertGreater(incremental["reused_files"], 0)
        detail = explain(self.repo.path, "pkg.helpers.run")
        self.assertEqual(len(detail["incoming_calls"]), 1)
        self.assertGreater(first["symbols"], 0)

    def test_incremental_matches_clean_after_changes(self) -> None:
        build(self.repo.path)
        self.repo.write("backend/src/pkg/helpers.py", "def renamed():\n    return 2\n")
        self.repo.write("backend/src/pkg/service.py", "from .helpers import renamed\n\ndef work():\n    return renamed()\n")
        incremental = build(self.repo.path, incremental=True)
        incremental_bytes = self.artifacts()
        self.assertEqual(incremental["parsed_files"], 2)
        build(self.repo.path)
        self.assertEqual(incremental_bytes, self.artifacts())

    def test_delete_rename_corrupt_fallback_and_obsolete_cleanup(self) -> None:
        build(self.repo.path)
        (self.repo.path / ".code-index" / "obsolete.txt").write_text("old")
        (self.repo.path / ".code-index" / "manifest.json").write_text("not-json")
        build(self.repo.path, incremental=True)
        self.assertFalse((self.repo.path / ".code-index" / "obsolete.txt").exists())
        subprocess.run(["git", "mv", "backend/src/pkg/service.py", "backend/src/pkg/renamed.py"], cwd=self.repo.path, check=True)
        build(self.repo.path, incremental=True)
        manifest = json.loads((self.repo.path / ".code-index" / "manifest.json").read_text())
        paths = {item["path"] for item in manifest["tracked_files"]}
        self.assertNotIn("backend/src/pkg/service.py", paths)
        self.assertIn("backend/src/pkg/renamed.py", paths)

    def test_check_is_read_only_and_detects_edits(self) -> None:
        build(self.repo.path)
        before = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo.path, text=True, capture_output=True, check=True).stdout
        self.assertEqual(check(self.repo.path), [])
        after = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo.path, text=True, capture_output=True, check=True).stdout
        self.assertEqual(before, after)
        with (self.repo.path / ".code-index" / "symbols.jsonl").open("ab") as handle:
            handle.write(b"{}\n")
        self.assertEqual(check(self.repo.path), [".code-index/symbols.jsonl"])

    def test_pre_commit_requires_staging(self) -> None:
        build(self.repo.path)
        self.assertTrue(pre_commit(self.repo.path))
        subprocess.run(["git", "add", ".code-index"], cwd=self.repo.path, check=True)
        self.assertEqual(pre_commit(self.repo.path), [])
        self.repo.write("backend/src/pkg/helpers.py", "def run():\n    return 5\n")
        self.assertTrue(pre_commit(self.repo.path))

    def test_parser_api_mismatch_is_rejected(self) -> None:
        class BadParser:
            language = "bad"
            api_version = "0"

        with mock.patch("agentic_os.code_index.core.PythonParser", return_value=BadParser()):
            with self.assertRaisesRegex(IndexError, "parser API mismatch"):
                build(self.repo.path)


class TypeScriptConnectionTests(unittest.TestCase):
    def test_frontend_fetch_resolves_to_unique_backend_route(self) -> None:
        repo = Repository()
        try:
            repo.write("backend/src/api.py", '''from fastapi import FastAPI\napp = FastAPI()\n@app.get("/api/goals")\ndef list_goals():\n    return []\n''')
            repo.write("frontend/app/page.tsx", '''export async function loadGoals(): Promise<void> {\n  await fetch("/api/goals")\n}\n''')
            repo.commit()
            compiler = Path(__file__).parents[2] / "frontend" / "node_modules"
            if not compiler.exists():
                self.skipTest("workspace TypeScript compiler is unavailable")
            (repo.path / "frontend" / "node_modules").symlink_to(compiler, target_is_directory=True)
            build(repo.path)
            dependencies = [json.loads(line) for line in (repo.path / ".code-index" / "dependencies.jsonl").read_text().splitlines()]
            fetch = next(item for item in dependencies if item["kind"] == "call" and item["target"] == "fetch")
            self.assertEqual(fetch["evidence"], "resolved")
            self.assertTrue(fetch["target_id"].startswith("python:function:"))
            self.assertEqual(fetch["extensions"]["core"]["connection"], "http")
        finally:
            repo.close()


if __name__ == "__main__":
    unittest.main()
