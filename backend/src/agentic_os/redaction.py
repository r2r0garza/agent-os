from __future__ import annotations

from typing import Any

SENSITIVE_FRAGMENTS = (
    "authorization",
    "api-key",
    "api_key",
    "cookie",
    "credential",
    "material",
    "password",
    "secret",
    "token",
)

NON_SENSITIVE_KEYS = frozenset(
    {
        "credential_configured",
        "credential_id",
        "credential_ids",
        "credential_scope_required",
        "credential_type",
    }
)

NON_SENSITIVE_TOKEN_KEYS = frozenset(
    {
        "completion_tokens",
        "input_cost_per_million_tokens",
        "input_price_per_million_tokens",
        "input_tokens",
        "max_tokens",
        "maximum_tokens",
        "output_cost_per_million_tokens",
        "output_price_per_million_tokens",
        "output_tokens",
        "prompt_tokens",
        "token_usage",
        "total_tokens",
    }
)


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).lower()
    if normalized in NON_SENSITIVE_KEYS or normalized in NON_SENSITIVE_TOKEN_KEYS:
        return False
    return any(fragment in normalized for fragment in SENSITIVE_FRAGMENTS)


def redact_mapping(value: Any) -> Any:
    """Recursively redact secret-bearing mapping keys before persistence or export."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_sensitive_key(key) else redact_mapping(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_mapping(item) for item in value]
    return value
