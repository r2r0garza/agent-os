from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any

MAX_INSTRUCTIONS_BYTES = 256 * 1024
MAX_RESOURCE_BYTES = 256 * 1024
MAX_PACKAGE_RESOURCE_BYTES = 1024 * 1024
MAX_RESOURCES = 100
EXPORT_EXCLUDED_KEY_FRAGMENTS = (
    "api_key",
    "api-key",
    "authorization",
    "cookie",
    "credential",
    "grant",
    "password",
    "secret",
    "token",
)
EXPORT_EXCLUDED_KEYS = frozenset({"run", "run_id", "run_state"})


def validate_and_normalize_skill_package(
    *,
    manifest: dict[str, Any] | None,
    instructions: str | None,
    resources: list[dict[str, Any]] | None,
    declared_capabilities: list[str] | None,
    provenance: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Validate package input and return canonical, content-hashed package data."""

    diagnostics: list[dict[str, Any]] = []
    if manifest is None:
        diagnostics.append(_diagnostic("manifest_required", "manifest", "package manifest is required"))
    else:
        for field in ("name", "description"):
            value = manifest.get(field)
            if not isinstance(value, str) or not value.strip():
                diagnostics.append(
                    _diagnostic(
                        "required_manifest_field",
                        f"manifest.{field}",
                        f"manifest field {field!r} must be a non-empty string",
                    )
                )

    if not isinstance(instructions, str) or not instructions.strip():
        diagnostics.append(
            _diagnostic("instructions_required", "instructions", "package instructions are required")
        )
    elif len(instructions.encode("utf-8")) > MAX_INSTRUCTIONS_BYTES:
        diagnostics.append(
            _diagnostic(
                "instructions_too_large",
                "instructions",
                f"instructions must not exceed {MAX_INSTRUCTIONS_BYTES} bytes",
            )
        )

    capabilities = declared_capabilities or []
    seen_capabilities: set[str] = set()
    normalized_capabilities: list[str] = []
    for index, capability in enumerate(capabilities):
        if not isinstance(capability, str) or not capability.strip():
            diagnostics.append(
                _diagnostic(
                    "invalid_capability",
                    f"declared_capabilities.{index}",
                    "capability names must be non-empty strings",
                )
            )
            continue
        normalized = capability.strip()
        if normalized in seen_capabilities:
            diagnostics.append(
                _diagnostic(
                    "duplicate_capability",
                    f"declared_capabilities.{index}",
                    f"capability {normalized!r} is declared more than once",
                )
            )
            continue
        seen_capabilities.add(normalized)
        normalized_capabilities.append(normalized)

    package_resources = resources or []
    if len(package_resources) > MAX_RESOURCES:
        diagnostics.append(
            _diagnostic(
                "too_many_resources",
                "resources",
                f"packages may contain at most {MAX_RESOURCES} resources",
            )
        )

    normalized_resources: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    total_resource_bytes = 0
    for index, resource in enumerate(package_resources):
        path = resource.get("path")
        content = resource.get("content")
        location = f"resources.{index}"
        if not isinstance(path, str) or not _is_safe_resource_path(path):
            diagnostics.append(
                _diagnostic(
                    "unsafe_resource_path",
                    f"{location}.path",
                    "resource paths must be normalized, relative POSIX paths",
                )
            )
            continue
        if path in seen_paths:
            diagnostics.append(
                _diagnostic(
                    "duplicate_resource_path",
                    f"{location}.path",
                    f"resource path {path!r} is included more than once",
                )
            )
            continue
        seen_paths.add(path)
        if not isinstance(content, str):
            diagnostics.append(
                _diagnostic(
                    "invalid_resource_content",
                    f"{location}.content",
                    "resource content must be a UTF-8 string",
                )
            )
            continue
        content_bytes = content.encode("utf-8")
        total_resource_bytes += len(content_bytes)
        if len(content_bytes) > MAX_RESOURCE_BYTES:
            diagnostics.append(
                _diagnostic(
                    "resource_too_large",
                    f"{location}.content",
                    f"each resource must not exceed {MAX_RESOURCE_BYTES} bytes",
                )
            )
        normalized_resources.append(
            {
                "path": path,
                "content": content,
                "media_type": resource.get("media_type") or "text/plain",
                "size_bytes": len(content_bytes),
                "sha256": hashlib.sha256(content_bytes).hexdigest(),
                "metadata": resource.get("metadata") or {},
            }
        )
    if total_resource_bytes > MAX_PACKAGE_RESOURCE_BYTES:
        diagnostics.append(
            _diagnostic(
                "package_resources_too_large",
                "resources",
                f"combined resource content must not exceed {MAX_PACKAGE_RESOURCE_BYTES} bytes",
            )
        )

    references = manifest.get("resources", []) if isinstance(manifest, dict) else []
    if not isinstance(references, list):
        diagnostics.append(
            _diagnostic(
                "malformed_resource_references",
                "manifest.resources",
                "manifest resource references must be a list of paths",
            )
        )
    else:
        for index, reference in enumerate(references):
            if not isinstance(reference, str) or not _is_safe_resource_path(reference):
                diagnostics.append(
                    _diagnostic(
                        "malformed_resource_reference",
                        f"manifest.resources.{index}",
                        "resource references must be normalized, relative POSIX paths",
                    )
                )
            elif reference not in seen_paths:
                diagnostics.append(
                    _diagnostic(
                        "missing_resource_reference",
                        f"manifest.resources.{index}",
                        f"referenced resource {reference!r} was not supplied",
                    )
                )

    if diagnostics:
        return None, diagnostics

    package = {
        "manifest": redact_skill_package_export(dict(manifest or {})),
        "instructions": instructions,
        "resources": redact_skill_package_export(
            sorted(normalized_resources, key=lambda item: item["path"])
        ),
        "declared_capabilities": sorted(normalized_capabilities),
        "provenance": redact_skill_package_export(
            provenance or {"source": "authored"}
        ),
    }
    package["package_hash"] = hashlib.sha256(
        json.dumps(package, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return package, []


def redact_skill_package_export(value: Any) -> Any:
    """Remove credential, grant, secret, and run-state fields from a bundle."""

    if isinstance(value, dict):
        return {
            key: redact_skill_package_export(item)
            for key, item in value.items()
            if not _is_export_excluded_key(key)
        }
    if isinstance(value, list):
        return [redact_skill_package_export(item) for item in value]
    return value


def _diagnostic(code: str, location: str, message: str) -> dict[str, str]:
    return {"code": code, "location": location, "message": message}


def _is_safe_resource_path(value: str) -> bool:
    if not value or "\\" in value or value.startswith("/") or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return (
        value == path.as_posix()
        and value not in {".", ".."}
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _is_export_excluded_key(key: object) -> bool:
    normalized = str(key).lower()
    return normalized in EXPORT_EXCLUDED_KEYS or any(
        fragment in normalized for fragment in EXPORT_EXCLUDED_KEY_FRAGMENTS
    )
