from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

SCHEMA_VERSION = "1.0.0"
GENERATOR_VERSION = "1.0.0"
PARSER_API_VERSION = "1.0.0"
REQUIRED_ARTIFACTS = ("schema.json", "manifest.json", "symbols.jsonl", "dependencies.jsonl")


class IndexError(RuntimeError):
    pass


@dataclass(frozen=True)
class TrackedFile:
    path: str
    size: int
    sha256: str
    content: bytes

    def manifest_record(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


class Parser(Protocol):
    language: str
    api_version: str

    def supports(self, path: str) -> bool: ...

    def parse(self, repository: Path, file: TrackedFile) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]: ...


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_bytes(value: Any) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def jsonl_bytes(records: list[dict[str, Any]]) -> bytes:
    return "".join(f"{canonical_json(record)}\n" for record in sorted(records, key=canonical_json)).encode("utf-8")


def stable_id(language: str, kind: str, qualified_name: str) -> str:
    digest = hashlib.sha256(canonical_json([language, kind, qualified_name]).encode()).hexdigest()
    return f"{language}:{kind}:{digest}"


def validate_path(path: str) -> str:
    if not path or "\\" in path or Path(path).is_absolute():
        raise IndexError(f"unsafe repository path: {path!r}")
    pure = PurePosixPath(path)
    if any(part in ("", ".", "..") for part in pure.parts):
        raise IndexError(f"unsafe repository path: {path!r}")
    return pure.as_posix()


def span(path: str, start_line: int, start_column: int, end_line: int, end_column: int) -> dict[str, Any]:
    return {
        "path": validate_path(path),
        "start": {"line": start_line, "column": start_column},
        "end": {"line": end_line, "column": end_column},
    }


def configuration() -> dict[str, Any]:
    return {
        "include_suffixes": [".js", ".jsx", ".mjs", ".mts", ".py", ".ts", ".tsx"],
        "exclude_parts": [
            ".code-index", ".git", ".mypy_cache", ".next", ".pytest_cache", ".ruff_cache",
            ".venv", "__pycache__", "build", "coverage", "dist", "node_modules", "out",
        ],
        "max_file_size": 1_000_000,
        "typescript_source_roots": ["frontend"],
        "javascript_source_roots": ["frontend", "backend/src/agentic_os/code_index"],
        "python_source_roots": ["backend/src", "backend/tests"],
    }


def configuration_fingerprint() -> str:
    return hashlib.sha256(canonical_json(configuration()).encode()).hexdigest()
