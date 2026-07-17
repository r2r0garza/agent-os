from __future__ import annotations

from typing import Any, Callable

ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


class ToolNotFoundError(KeyError):
    """Raised when a run requests a tool with no governed handler."""


def _echo_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    return {"echo": arguments}


TOOL_REGISTRY: dict[str, ToolHandler] = {
    "echo": _echo_tool,
}

BUILTIN_TOOL_DESCRIPTORS: dict[str, dict[str, Any]] = {
    "echo": {
        "name": "echo",
        "description": "Return the supplied governed arguments.",
        "input_schema": {
            "type": "object",
            "additionalProperties": True,
        },
    },
}


def invoke_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        handler = TOOL_REGISTRY[name]
    except KeyError as error:
        raise ToolNotFoundError(f"no governed tool registered for {name!r}") from error
    return handler(arguments)
