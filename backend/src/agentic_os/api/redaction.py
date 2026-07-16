from __future__ import annotations

from typing import Any

SENSITIVE_FRAGMENTS = (
    "authorization",
    "api-key",
    "api_key",
    "cookie",
    "material",
    "password",
    "secret",
    "token",
)


def redact_mapping(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if any(fragment in str(key).lower() for fragment in SENSITIVE_FRAGMENTS)
                else redact_mapping(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_mapping(item) for item in value]
    return value
