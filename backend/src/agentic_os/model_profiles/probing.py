from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

CAPABILITY_NAMES = (
    "streaming",
    "tool_calls",
    "structured_output",
    "token_usage",
    "reasoning",
    "retry_timeout",
)


@dataclass(frozen=True)
class ProbeSettings:
    timeout_seconds: float = 2.0
    max_attempts: int = 2


def _capability(status: str, diagnostic: str) -> dict[str, str]:
    return {"status": status, "diagnostic": diagnostic}


def _safe_endpoint(base_url: str) -> str:
    parts = urlsplit(base_url)
    hostname = parts.hostname or ""
    if parts.port is not None:
        hostname = f"{hostname}:{parts.port}"
    return urlunsplit((parts.scheme, hostname, parts.path.rstrip("/"), "", ""))


def _chat_endpoint(base_url: str) -> str:
    parts = urlsplit(base_url)
    path = f"{parts.path.rstrip('/')}/chat/completions"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _pricing_evidence(metadata: dict[str, Any]) -> dict[str, Any]:
    if metadata.get("chargeable") is False:
        return {
            "status": "valid",
            "metered": False,
            "warnings": [],
            "failures": [],
        }

    aliases = {
        "input": ("input_cost_per_million_tokens", "input_price_per_million_tokens"),
        "output": ("output_cost_per_million_tokens", "output_price_per_million_tokens"),
    }
    missing = [
        direction
        for direction, keys in aliases.items()
        if not any(metadata.get(key) is not None for key in keys)
    ]
    invalid = []
    for key in (*aliases["input"], *aliases["output"]):
        value = metadata.get(key)
        if value is not None and (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value < 0
        ):
            invalid.append(key)

    failures = []
    warnings = []
    if missing:
        failures.append(
            {
                "code": "unpriced_metered_action",
                "message": f"missing {' and '.join(missing)} token pricing",
            }
        )
    if invalid:
        failures.append(
            {
                "code": "invalid_pricing_metadata",
                "message": f"non-negative numeric pricing required for: {', '.join(sorted(invalid))}",
            }
        )
    if not metadata.get("currency"):
        warnings.append(
            {
                "code": "pricing_currency_unknown",
                "message": "pricing currency is not declared",
            }
        )
    return {
        "status": "error" if failures else ("warning" if warnings else "valid"),
        "metered": True,
        "warnings": warnings,
        "failures": failures,
    }


def _post_with_retry(
    client: httpx.Client,
    endpoint: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    settings: ProbeSettings,
) -> tuple[httpx.Response | None, int, str | None]:
    attempts = 0
    while attempts < settings.max_attempts:
        attempts += 1
        try:
            return (
                client.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                    timeout=settings.timeout_seconds,
                ),
                attempts,
                None,
            )
        except httpx.TimeoutException:
            if attempts >= settings.max_attempts:
                return None, attempts, "timeout"
        except httpx.HTTPError:
            return None, attempts, "connection_error"
    return None, attempts, "connection_error"


def _unsupported_or_unknown(
    response: httpx.Response | None, error: str | None
) -> dict[str, str]:
    if response is not None and response.status_code in {400, 404, 405, 415, 422, 501}:
        return _capability("unsupported", f"provider returned HTTP {response.status_code}")
    if response is not None:
        return _capability("unknown", f"provider returned HTTP {response.status_code}")
    return _capability("unknown", error or "provider request failed")


def _base_payload(model_identifier: str) -> dict[str, Any]:
    return {
        "model": model_identifier,
        "messages": [{"role": "user", "content": "Reply with the word probe."}],
        "max_tokens": 16,
    }


def probe_model_profile(
    *,
    base_url: str,
    model_identifier: str,
    api_key: str,
    configured_headers: dict[str, Any] | None,
    pricing_metadata: dict[str, Any] | None,
    settings: ProbeSettings | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    settings = settings or ProbeSettings()
    started_at = datetime.now(UTC)
    headers = {str(key): str(value) for key, value in (configured_headers or {}).items()}
    if not any(key.lower() == "authorization" for key in headers):
        headers["Authorization"] = f"Bearer {api_key}"
    headers.setdefault("Content-Type", "application/json")
    endpoint = _chat_endpoint(base_url)
    request_metadata = {
        "endpoint": _safe_endpoint(base_url),
        "path": "/chat/completions",
        "method": "POST",
        "model_identifier": model_identifier,
        "header_names": sorted(headers),
        "timeout_seconds": settings.timeout_seconds,
        "max_attempts": settings.max_attempts,
    }
    capabilities = {
        name: _capability("unknown", "probe did not complete")
        for name in CAPABILITY_NAMES
    }
    diagnostics: list[dict[str, Any]] = []
    owns_client = client is None
    http_client = client or httpx.Client()

    try:
        base_response, attempts, base_error = _post_with_retry(
            http_client,
            endpoint,
            headers=headers,
            payload=_base_payload(model_identifier),
            settings=settings,
        )
        request_metadata["attempts"] = attempts
        capabilities["retry_timeout"] = (
            _capability(
                "supported",
                (
                    "request succeeded after retry"
                    if attempts > 1
                    else "bounded timeout policy completed"
                ),
            )
            if base_response is not None and base_response.is_success
            else _capability("unknown", base_error or "base request did not succeed")
        )
        if base_response is None:
            diagnostics.append({"code": base_error or "request_failed", "phase": "base"})
            return _result(
                started_at,
                "failed",
                capabilities,
                _pricing_evidence(pricing_metadata or {}),
                request_metadata,
                diagnostics,
            )
        if not base_response.is_success:
            diagnostics.append(
                {
                    "code": "http_error",
                    "phase": "base",
                    "http_status": base_response.status_code,
                }
            )
            return _result(
                started_at,
                "failed",
                capabilities,
                _pricing_evidence(pricing_metadata or {}),
                request_metadata,
                diagnostics,
            )
        try:
            base_data = base_response.json()
            message = base_data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            diagnostics.append({"code": "malformed_response", "phase": "base"})
            return _result(
                started_at,
                "failed",
                capabilities,
                _pricing_evidence(pricing_metadata or {}),
                request_metadata,
                diagnostics,
            )

        usage = base_data.get("usage")
        capabilities["token_usage"] = (
            _capability("supported", "usage object returned")
            if isinstance(usage, dict)
            and any(
                key in usage
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            )
            else _capability("unsupported", "successful response omitted token usage")
        )
        capabilities["reasoning"] = (
            _capability("supported", "reasoning field returned")
            if any(key in message for key in ("reasoning", "reasoning_content"))
            else _capability("unknown", "successful response did not include reasoning fields")
        )

        structured_payload = {
            **_base_payload(model_identifier),
            "messages": [{"role": "user", "content": 'Return JSON: {"probe": true}'}],
            "response_format": {"type": "json_object"},
        }
        structured_response, _, structured_error = _post_with_retry(
            http_client, endpoint, headers=headers, payload=structured_payload, settings=settings
        )
        if structured_response is not None and structured_response.is_success:
            try:
                content = structured_response.json()["choices"][0]["message"]["content"]
                json.loads(content)
                capabilities["structured_output"] = _capability(
                    "supported", "valid JSON object returned"
                )
            except (ValueError, KeyError, IndexError, TypeError):
                capabilities["structured_output"] = _capability(
                    "unknown", "request succeeded without a valid structured response"
                )
        else:
            capabilities["structured_output"] = _unsupported_or_unknown(
                structured_response, structured_error
            )

        tool_payload = {
            **_base_payload(model_identifier),
            "messages": [{"role": "user", "content": "Call the probe tool."}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "probe_capability",
                        "description": "A no-side-effect compatibility probe.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "probe_capability"}},
        }
        tool_response, _, tool_error = _post_with_retry(
            http_client, endpoint, headers=headers, payload=tool_payload, settings=settings
        )
        if tool_response is not None and tool_response.is_success:
            try:
                tool_calls = tool_response.json()["choices"][0]["message"]["tool_calls"]
                capabilities["tool_calls"] = (
                    _capability("supported", "tool call returned")
                    if isinstance(tool_calls, list) and tool_calls
                    else _capability("unknown", "request succeeded without a tool call")
                )
            except (ValueError, KeyError, IndexError, TypeError):
                capabilities["tool_calls"] = _capability(
                    "unknown", "request succeeded without parseable tool-call evidence"
                )
        else:
            capabilities["tool_calls"] = _unsupported_or_unknown(tool_response, tool_error)

        stream_payload = {**_base_payload(model_identifier), "stream": True}
        stream_response, _, stream_error = _post_with_retry(
            http_client, endpoint, headers=headers, payload=stream_payload, settings=settings
        )
        if stream_response is not None and stream_response.is_success:
            content_type = stream_response.headers.get("content-type", "")
            has_data_frame = any(
                line.startswith("data:")
                for line in stream_response.text.splitlines()
            )
            capabilities["streaming"] = (
                _capability("supported", "SSE data frame returned")
                if "text/event-stream" in content_type and has_data_frame
                else _capability("unknown", "request succeeded without SSE evidence")
            )
        else:
            capabilities["streaming"] = _unsupported_or_unknown(stream_response, stream_error)

        pricing = _pricing_evidence(pricing_metadata or {})
        degraded = pricing["status"] != "valid" or any(
            item["status"] != "supported"
            for key, item in capabilities.items()
            if key not in {"reasoning"}
        )
        return _result(
            started_at,
            "degraded" if degraded else "completed",
            capabilities,
            pricing,
            request_metadata,
            diagnostics,
        )
    finally:
        if owns_client:
            http_client.close()


def _result(
    started_at: datetime,
    status: str,
    capabilities: dict[str, dict[str, str]],
    pricing: dict[str, Any],
    request_metadata: dict[str, Any],
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "status": status,
        "started_at": started_at,
        "completed_at": datetime.now(UTC),
        "capability_evidence": capabilities,
        "pricing_evidence": pricing,
        "request_metadata": request_metadata,
        "diagnostics": diagnostics,
    }
