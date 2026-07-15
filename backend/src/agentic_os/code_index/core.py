from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from agentic_os.code_index.contracts import (
    GENERATOR_VERSION, PARSER_API_VERSION, REQUIRED_ARTIFACTS, SCHEMA_VERSION, IndexError,
    canonical_json, configuration, configuration_fingerprint, json_bytes, jsonl_bytes,
)
from agentic_os.code_index.discovery import discover
from agentic_os.code_index.python_parser import PythonParser
from agentic_os.code_index.resolver import resolve
from agentic_os.code_index.typescript_parser import JavaScriptParser, TypeScriptParser


def _schema() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "parser_api_version": PARSER_API_VERSION,
        "contracts": {
            "serialization": "UTF-8, canonical sorted-key compact JSON, LF, one final newline per record",
            "paths": "Git repository-relative POSIX paths",
            "spans": "one-based columns and lines; end positions are inclusive of the represented syntactic span",
            "stable_id": "{language}:{kind}:SHA-256(canonical JSON([language,kind,qualified_name]))",
            "symbol_kinds": ["module", "class", "function", "method"],
            "dependency_kinds": ["import", "call"],
            "evidence": ["declared", "resolved", "inferred", "unresolved"],
            "resolved_call": "evidence is resolved and target_id uniquely identifies an indexed symbol",
            "unresolved_call": "evidence is unresolved and target_id is omitted",
            "extensions": "language-specific fields are namespaced; core contains source path and resolver evidence",
            "cross_language": "literal frontend HTTP calls resolve to unique decorated backend routes as call edges",
            "records": {
                "manifest": {
                    "required": ["schema_version", "generator_version", "parser_api_version", "configuration_fingerprint", "aggregate_content_hash", "tracked_files", "artifact_counts"],
                    "tracked_file_required": ["path", "size", "sha256"],
                },
                "symbol": {
                    "required": ["id", "language", "kind", "qualified_name", "span", "extensions"],
                    "optional": ["signature", "visibility"],
                },
                "dependency": {
                    "required": ["language", "kind", "source_id", "target", "evidence", "span", "extensions"],
                    "optional": ["target_id"],
                },
            },
        },
    }


def _parsers() -> list[Any]:
    parsers = [PythonParser(), TypeScriptParser(), JavaScriptParser()]
    for parser in parsers:
        if parser.api_version != PARSER_API_VERSION:
            raise IndexError(f"parser API mismatch for {parser.language}: {parser.api_version}")
    return parsers


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _load_previous(output: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]] | None:
    try:
        if not all((output / name).is_file() for name in REQUIRED_ARTIFACTS):
            return None
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        expected = {
            "schema_version": SCHEMA_VERSION, "generator_version": GENERATOR_VERSION,
            "parser_api_version": PARSER_API_VERSION, "configuration_fingerprint": configuration_fingerprint(),
        }
        if any(manifest.get(key) != value for key, value in expected.items()):
            return None
        return manifest, _read_jsonl(output / "symbols.jsonl"), _read_jsonl(output / "dependencies.jsonl")
    except (OSError, ValueError, TypeError):
        return None


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def build(repository: Path, incremental: bool = False, output: Path | None = None) -> dict[str, int]:
    repository = repository.resolve()
    output = output or repository / ".code-index"
    tracked = discover(repository)
    previous = _load_previous(output) if incremental else None
    reuse_enabled = incremental and previous is not None
    previous_hashes = {item["path"]: item["sha256"] for item in previous[0]["tracked_files"]} if previous else {}
    unchanged = {item.path for item in tracked if previous_hashes.get(item.path) == item.sha256}
    symbols = [item for item in previous[1] if item["extensions"]["core"]["path"] in unchanged] if previous else []
    dependencies = [item for item in previous[2] if item["extensions"]["core"]["path"] in unchanged] if previous else []
    parsers = _parsers()
    parsed_files = 0
    for file in tracked:
        owners = [parser for parser in parsers if parser.supports(file.path)]
        if len(owners) != 1:
            raise IndexError(f"expected exactly one parser for {file.path}, found {len(owners)}")
        if file.path in unchanged:
            continue
        file_symbols, file_dependencies = owners[0].parse(repository, file)
        symbols.extend(file_symbols)
        dependencies.extend(file_dependencies)
        parsed_files += 1
    for file in tracked:
        try:
            current_content = (repository / file.path).read_bytes()
        except OSError as error:
            raise IndexError(f"tracked file changed during indexing: {file.path}: {error}") from error
        if hashlib.sha256(current_content).hexdigest() != file.sha256:
            raise IndexError(f"tracked file changed during indexing: {file.path}")
    dependencies = resolve(symbols, dependencies)
    tracked_records = [item.manifest_record() for item in tracked]
    aggregate = hashlib.sha256(canonical_json(tracked_records).encode("utf-8")).hexdigest()
    counts = {
        "symbols": len(symbols), "dependencies": len(dependencies), "resolved_dependencies": sum("target_id" in item for item in dependencies),
        "parsed_files": parsed_files, "reused_files": len(unchanged),
    }
    manifest = {
        "schema_version": SCHEMA_VERSION, "generator_version": GENERATOR_VERSION,
        "parser_api_version": PARSER_API_VERSION, "configuration_fingerprint": configuration_fingerprint(),
        "aggregate_content_hash": aggregate, "tracked_files": tracked_records,
        "artifact_counts": {key: counts[key] for key in ("symbols", "dependencies", "resolved_dependencies")},
    }
    output.mkdir(parents=True, exist_ok=True)
    if not reuse_enabled:
        for child in output.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    artifacts = {
        "schema.json": json_bytes(_schema()), "manifest.json": json_bytes(manifest),
        "symbols.jsonl": jsonl_bytes(symbols), "dependencies.jsonl": jsonl_bytes(dependencies),
    }
    for name in REQUIRED_ARTIFACTS:
        _atomic_write(output / name, artifacts[name])
    return counts


def check(repository: Path) -> list[str]:
    repository = repository.resolve()
    with tempfile.TemporaryDirectory(prefix="agentic-os-index-check-") as directory:
        candidate = Path(directory) / ".code-index"
        build(repository, output=candidate)
        stale = []
        for name in REQUIRED_ARTIFACTS:
            committed = repository / ".code-index" / name
            if not committed.is_file() or committed.read_bytes() != (candidate / name).read_bytes():
                stale.append(f".code-index/{name}")
        return stale


def explain(repository: Path, qualified_name: str) -> dict[str, Any]:
    output = repository.resolve() / ".code-index"
    try:
        symbols = _read_jsonl(output / "symbols.jsonl")
        dependencies = _read_jsonl(output / "dependencies.jsonl")
    except (OSError, ValueError) as error:
        raise IndexError(f"cannot read existing index: {error}") from error
    matches = [item for item in symbols if item["qualified_name"] == qualified_name]
    if not matches:
        raise IndexError(f"symbol does not exist: {qualified_name}")
    if len(matches) != 1:
        raise IndexError(f"ambiguous qualified name: {qualified_name}")
    symbol = matches[0]
    relationships = [item for item in dependencies if item["source_id"] == symbol["id"]]
    return {
        "symbol": symbol, "relationships": relationships,
        "outgoing_calls": [item for item in relationships if item["kind"] == "call"],
        "incoming_calls": [item for item in dependencies if item["kind"] == "call" and item.get("target_id") == symbol["id"]],
    }


def pre_commit(repository: Path) -> list[str]:
    repository = repository.resolve()
    build(repository, incremental=True)
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--", ".code-index"], cwd=repository, check=True,
        capture_output=True, text=True,
    )
    unstaged = []
    for line in result.stdout.splitlines():
        if line.startswith("??") or (len(line) > 1 and line[1] != " "):
            unstaged.append(line[3:])
    return sorted(unstaged)
