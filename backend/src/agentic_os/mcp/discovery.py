from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

MAX_DESCRIPTION_LENGTH = 512


@dataclass(frozen=True)
class DiscoverySettings:
    timeout_seconds: float = 2.0
    max_attempts: int = 2


def _safe_endpoint(url: str) -> str:
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    if parts.port is not None:
        hostname = f"{hostname}:{parts.port}"
    return urlunsplit((parts.scheme, hostname, parts.path.rstrip("/"), "", ""))


def _safe_headers(headers: dict[str, Any]) -> list[str]:
    return sorted(str(key) for key in headers)


def _descriptor_hash(name: str, description: str, input_schema: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"name": name, "description": description, "input_schema": input_schema},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validate_schema(input_schema: Any) -> tuple[bool, list[str]]:
    if input_schema in (None, {}):
        return True, []
    if not isinstance(input_schema, dict):
        return False, ["inputSchema must be a JSON object"]
    if input_schema.get("type") != "object":
        return False, ["inputSchema must declare type 'object'"]
    return True, []


def _normalize_tool(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    description = raw.get("description")
    description = description[:MAX_DESCRIPTION_LENGTH] if isinstance(description, str) else ""
    input_schema = raw.get("inputSchema", raw.get("input_schema")) or {}
    schema_valid, schema_validation_errors = _validate_schema(input_schema)
    if not isinstance(input_schema, dict):
        input_schema = {}
    credential_scope_required = bool(
        raw.get("credentialScopeRequired", raw.get("credential_scope_required", False))
    )
    return {
        "name": name,
        "description": description,
        "input_schema": input_schema,
        "schema_valid": schema_valid,
        "schema_validation_errors": schema_validation_errors,
        "descriptor_hash": _descriptor_hash(name, description, input_schema),
        "credential_scope_required": credential_scope_required,
    }


def _post_with_retry(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    settings: DiscoverySettings,
) -> tuple[httpx.Response | None, int, str | None]:
    attempts = 0
    while attempts < settings.max_attempts:
        attempts += 1
        try:
            return (
                client.post(url, headers=headers, json=payload, timeout=settings.timeout_seconds),
                attempts,
                None,
            )
        except httpx.TimeoutException:
            if attempts >= settings.max_attempts:
                return None, attempts, "timeout"
        except httpx.HTTPError:
            return None, attempts, "connection_error"
    return None, attempts, "connection_error"


def discover_mcp_tools(
    *,
    url: str | None,
    headers: dict[str, Any] | None,
    credential_value: str | None,
    settings: DiscoverySettings | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Discover tool descriptors from a configured MCP server over HTTP.

    Tool descriptions returned by the remote server are untrusted display and
    schema evidence only; this function never marks a tool `enabled` and
    never resolves policy, credential scope, or budget authority.
    """
    settings = settings or DiscoverySettings()
    started_at = datetime.now(UTC)
    request_headers = {str(key): str(value) for key, value in (headers or {}).items()}
    if credential_value and not any(key.lower() == "authorization" for key in request_headers):
        request_headers["Authorization"] = f"Bearer {credential_value}"
    request_headers.setdefault("Content-Type", "application/json")
    request_metadata = {
        "endpoint": _safe_endpoint(url) if url else None,
        "header_names": _safe_headers(request_headers),
        "timeout_seconds": settings.timeout_seconds,
        "max_attempts": settings.max_attempts,
    }

    if not url:
        return _result(
            started_at, "unreachable", 0, None, request_metadata,
            [{"code": "missing_url", "phase": "configuration"}],
        )

    owns_client = client is None
    http_client = client or httpx.Client()
    try:
        response, attempts, error = _post_with_retry(
            http_client,
            url,
            headers=request_headers,
            payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            settings=settings,
        )
        request_metadata["attempts"] = attempts
        latency_ms = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
        if response is None:
            return _result(
                started_at, "unreachable", 0, latency_ms, request_metadata,
                [{"code": error or "connection_error", "phase": "request"}],
            )
        if not response.is_success:
            return _result(
                started_at, "unreachable", 0, latency_ms, request_metadata,
                [{"code": "http_error", "phase": "request", "http_status": response.status_code}],
            )
        try:
            body = response.json()
        except ValueError:
            return _result(
                started_at, "malformed", 0, latency_ms, request_metadata,
                [{"code": "invalid_json", "phase": "response"}],
            )
        if not isinstance(body, dict):
            return _result(
                started_at, "malformed", 0, latency_ms, request_metadata,
                [{"code": "malformed_response", "phase": "response"}],
            )
        result = body.get("result") if isinstance(body.get("result"), dict) else body
        raw_tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(raw_tools, list):
            return _result(
                started_at, "malformed", 0, latency_ms, request_metadata,
                [{"code": "missing_tools_list", "phase": "response"}],
            )

        diagnostics: list[dict[str, Any]] = []
        tools: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_tools):
            normalized = _normalize_tool(raw)
            if normalized is None:
                diagnostics.append(
                    {"code": "invalid_tool_descriptor", "phase": "response", "index": index}
                )
                continue
            if not normalized["schema_valid"]:
                diagnostics.append(
                    {
                        "code": "invalid_tool_schema",
                        "phase": "response",
                        "tool": normalized["name"],
                        "errors": normalized["schema_validation_errors"],
                    }
                )
            tools.append(normalized)

        if not tools:
            status = "malformed" if raw_tools else "degraded"
        elif any(not tool["schema_valid"] for tool in tools) or len(tools) != len(raw_tools):
            status = "degraded"
        else:
            status = "healthy"

        return _result(started_at, status, len(tools), latency_ms, request_metadata, diagnostics, tools)
    finally:
        if owns_client:
            http_client.close()


def _result(
    started_at: datetime,
    status: str,
    tool_count: int,
    latency_ms: int | None,
    request_metadata: dict[str, Any],
    diagnostics: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "tool_count": tool_count,
        "latency_ms": latency_ms,
        "request_metadata": request_metadata,
        "diagnostics": diagnostics,
        "tools": tools or [],
        "checked_at": datetime.now(UTC),
        "started_at": started_at,
    }
