from __future__ import annotations

import posixpath
from collections import defaultdict
from typing import Any


def _unique(values: list[dict[str, Any]]) -> dict[str, Any] | None:
    return values[0] if len(values) == 1 else None


def _typescript_module(specifier: str, current: str) -> str | None:
    if specifier.startswith("@/"):
        return specifier[2:].replace("/", ".").removesuffix(".index")
    if specifier.startswith("."):
        current_path = current.replace(".", "/")
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(current_path), specifier))
        return joined.replace("/", ".").removesuffix(".index")
    return None


def _python_module(specifier: str, current: str) -> str:
    dots = len(specifier) - len(specifier.lstrip("."))
    if not dots:
        return specifier
    suffix = specifier[dots:]
    package = current.rpartition(".")[0].split(".") if "." in current else []
    keep = max(0, len(package) - dots + 1)
    parts = package[:keep] + ([suffix] if suffix else [])
    return ".".join(part for part in parts if part)


def resolve(symbols: list[dict[str, Any]], dependencies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_id = {item["id"]: item for item in symbols}
    for symbol in symbols:
        by_name[(symbol["language"], symbol["qualified_name"])].append(symbol)

    route_targets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for symbol in symbols:
        for route in symbol.get("extensions", {}).get("python", {}).get("routes", []):
            route_targets[(route["method"], route["path"])].append(symbol)

    resolved: list[dict[str, Any]] = []
    aliases: dict[tuple[str, str], dict[str, tuple[str | None, str | None, bool]]] = defaultdict(dict)
    for original in dependencies:
        item = {**original, "extensions": {key: dict(value) for key, value in original["extensions"].items()}}
        item.pop("target_id", None)
        details = item["extensions"]["core"]["resolution"]
        source = by_id[item["source_id"]]
        path = item["extensions"]["core"]["path"]
        if item["kind"] != "import":
            resolved.append(item)
            continue
        language = item["language"]
        current = source["qualified_name"] if source["kind"] == "module" else details.get("module", "")
        specifier = details["module"]
        repository_module = _python_module(specifier, current) if language == "python" else _typescript_module(specifier, current)
        target_name: str | None = repository_module
        if details["form"] in {"from", "named"} and repository_module:
            candidate = f"{repository_module}.{details['name']}"
            if by_name.get((language, candidate)):
                target_name = candidate
        candidate_symbol = _unique(by_name.get((language, target_name or ""), []))
        if language == "python":
            repository_declaration = bool(by_name.get((language, repository_module or ""))) or candidate_symbol is not None
            third_party = not specifier.startswith(".") and not repository_declaration
        else:
            third_party = repository_module is None
        if details["alias"]:
            aliases[(language, path)][details["alias"]] = (repository_module, target_name, third_party)
        if candidate_symbol:
            item["target_id"] = candidate_symbol["id"]
            item["evidence"] = "resolved"
        else:
            item["evidence"] = "declared"
        resolved.append(item)

    final: list[dict[str, Any]] = []
    for item in resolved:
        if item["kind"] != "call":
            final.append(item)
            continue
        details = item["extensions"]["core"]["resolution"]
        http = item["extensions"].get("typescript", {}).get("http")
        if http:
            target = _unique(route_targets.get((http["method"], http["path"]), []))
            if target:
                item["target_id"] = target["id"]
                item["evidence"] = "resolved"
                item["extensions"]["core"]["connection"] = "http"
                final.append(item)
                continue
        syntactic = details.get("syntactic_target") or details.get("syntacticTarget")
        language = item["language"]
        path = item["extensions"]["core"]["path"]
        current_module = details["module"]
        candidates: list[str] = []
        first, _, remainder = syntactic.partition(".")
        alias = aliases[(language, path)].get(first)
        if alias:
            repository_module, imported_name, third_party = alias
            if third_party:
                continue
            base = imported_name or repository_module
            if base:
                candidates.append(f"{base}.{remainder}" if remainder else base)
        if first in {"self", "this"} and details.get("class") and remainder:
            candidates.append(f"{details['class']}.{remainder}")
        if "." not in syntactic:
            owner = details.get("owner", current_module)
            pieces = owner.split(".")
            while pieces:
                candidates.append(".".join(pieces + [syntactic]))
                pieces.pop()
            candidates.append(f"{current_module}.{syntactic}")
        candidates.append(syntactic)
        matches: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            for symbol in by_name.get((language, candidate), []):
                matches[symbol["id"]] = symbol
        if len(matches) == 1:
            target = next(iter(matches.values()))
            item["target_id"] = target["id"]
            item["evidence"] = "resolved"
        else:
            item["evidence"] = "unresolved"
        final.append(item)
    return final
