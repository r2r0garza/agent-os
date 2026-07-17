from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.domain.models import ApprovalRequest, AuditEvent, Goal, Project, Run, Task
from agentic_os.observability import CorrelationContext, record_observability
from agentic_os.worker.configuration import (
    ConfigurationSnapshotError,
    ResolvedRunConfiguration,
)
from agentic_os.worker.governance import (
    BudgetActionContext,
    BudgetExhaustedError,
    evaluate_action_policy,
    mark_action_cost_uncertain,
    reconcile_action_cost,
    release_action_cost,
    reserve_action_cost,
)
from agentic_os.worker.tools import BUILTIN_TOOL_DESCRIPTORS, invoke_tool

DEFAULT_OUTPUT_LIMIT_BYTES = 64 * 1024
MAX_OUTPUT_LIMIT_BYTES = 1024 * 1024
MAX_DESCRIPTION_LENGTH = 512
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


class ToolBridgeError(RuntimeError):
    """Raised when the governed harness tool boundary rejects a call."""


class ToolBridgePolicyError(ToolBridgeError):
    """Raised before dispatch when current policy no longer allows a tool."""


class ToolBridgeApprovalRequired(ToolBridgeError):
    """Raised before dispatch when a new approval is required."""


class ToolBridgeOutputLimitError(ToolBridgeError):
    """Raised before dispatch when a configured output limit is invalid."""


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if any(fragment in str(key).lower() for fragment in SENSITIVE_FRAGMENTS)
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _safe_parameters(descriptor: dict[str, Any]) -> dict[str, Any]:
    candidate = descriptor.get("input_schema", descriptor.get("parameters"))
    if not isinstance(candidate, dict) or candidate.get("type") != "object":
        return {"type": "object", "additionalProperties": True}
    allowed = {
        key: copy.deepcopy(value)
        for key, value in candidate.items()
        if key in {"type", "properties", "required", "additionalProperties"}
    }
    allowed["type"] = "object"
    return allowed


def _safe_description(descriptor: dict[str, Any]) -> str:
    description = descriptor.get("description")
    if not isinstance(description, str):
        return "Governed Agentic OS tool."
    return description[:MAX_DESCRIPTION_LENGTH]


def _output_limit(descriptor: dict[str, Any]) -> int:
    raw = descriptor.get("output_limit_bytes", DEFAULT_OUTPUT_LIMIT_BYTES)
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise ToolBridgeOutputLimitError("tool output_limit_bytes must be an integer") from error
    if value <= 0:
        raise ToolBridgeOutputLimitError("tool output_limit_bytes must be greater than zero")
    return min(value, MAX_OUTPUT_LIMIT_BYTES)


def _bounded_result(result: dict[str, Any], limit: int) -> tuple[dict[str, Any], bool, int]:
    safe = _redact(result)
    encoded = json.dumps(safe, sort_keys=True, default=str).encode()
    if len(encoded) <= limit:
        return safe, False, len(encoded)
    preview = encoded[:limit].decode("utf-8", errors="ignore")
    return {
        "truncated": True,
        "preview": preview,
        "original_bytes": len(encoded),
        "output_limit_bytes": limit,
    }, True, len(encoded)


@dataclass
class GovernedToolBridge:
    session: Session
    resolved: ResolvedRunConfiguration
    task: Task
    run: Run
    project: Project
    goal: Goal
    context: CorrelationContext
    control_check: Callable[[], None] | None = None

    def descriptors(self) -> list[dict[str, Any]]:
        """Return normalized OpenAI tool schemas from the pinned snapshot.

        External MCP descriptions and schemas are treated as display/schema
        input only. They cannot add tools, change names, or carry policy
        instructions into the bridge.
        """
        result = []
        for name in self.resolved.enabled_tools:
            descriptor = self.resolved.tool_descriptor(name)
            try:
                _output_limit(descriptor)
            except ToolBridgeOutputLimitError as error:
                self._record_rejection(
                    name, "invalid_output_limit", reason=str(error)
                )
                raise
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": _safe_description(descriptor),
                        "parameters": _safe_parameters(descriptor),
                    },
                }
            )
        return result

    def skill_resources(self) -> list[dict[str, Any]]:
        result = []
        for skill in self.resolved.configuration["skills"]:
            if skill.get("grant_type") == "skill_resources":
                result.append(
                    _redact(
                        {
                            "skill_version_id": skill["id"],
                            "resource_paths": skill.get("resource_paths", []),
                            "resources": skill.get("resources", []),
                            "declared_capabilities": skill.get("declared_capabilities", []),
                            "provenance": skill.get("provenance", {}),
                        }
                    )
                )
            else:
                result.append(
                    _redact(
                        {
                            "skill_version_id": skill["id"],
                            "content_ref": skill["content_ref"],
                            "resource_metadata": skill.get("resource_metadata") or {},
                        }
                    )
                )
        return result

    def invoke(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name not in self.resolved.enabled_tools:
            self._record_rejection(tool_name, "not_enabled")
            raise ToolBridgePolicyError(
                f"tool {tool_name!r} is not enabled in snapshot {self.resolved.snapshot_id}"
            )
        if self.control_check is not None:
            self.control_check()

        descriptor = self.resolved.tool_descriptor(tool_name)
        output_limit = _output_limit(descriptor)
        try:
            self.resolved.validate_tool_access(
                self.session, tool_name=tool_name, project=self.project
            )
        except ConfigurationSnapshotError as error:
            reason_code = getattr(error, "reason_code", "credential_or_visibility_revoked")
            self._record_rejection(tool_name, reason_code, reason=str(error))
            raise ToolBridgePolicyError(str(error)) from error

        mcp_server_id = None
        for server in self.resolved.configuration["mcp_servers"]:
            if any(
                item.get("name") == tool_name
                for item in server["connection_config"].get("tools", [])
            ):
                mcp_server_id = uuid.UUID(server["mcp_server_id"])
                break
        agent_id = uuid.UUID(self.resolved.configuration["agent"]["id"])
        decision, evaluations = evaluate_action_policy(
            self.session, agent_id=agent_id, mcp_server_id=mcp_server_id
        )
        if decision == "deny":
            self._record_rejection(tool_name, "policy_denied", evaluations=evaluations)
            raise ToolBridgePolicyError(f"current policy denied tool {tool_name!r}")
        if decision == "approval_required" and not self._has_approved_request(tool_name):
            self._record_rejection(
                tool_name, "approval_required", evaluations=evaluations
            )
            raise ToolBridgeApprovalRequired(
                f"current policy requires approval for tool {tool_name!r}"
            )

        pricing = descriptor.get("pricing") or {}
        chargeable = pricing.get("chargeable") is True
        amount = (
            int(pricing["amount_minor_units"])
            if chargeable and pricing.get("amount_minor_units") is not None
            else None if chargeable else 0
        )
        budget = self.resolved.budget
        currency = pricing.get("currency", budget.currency if budget else "USD")
        budget_context = BudgetActionContext(
            team_id=self.project.team_id,
            project_id=self.project.id,
            goal_id=self.goal.id,
            task_id=self.task.id,
            run_id=self.run.id,
            agent_version_id=self.run.agent_version_id,
            requested_by=self.goal.created_by,
        )

        self.session.commit()
        try:
            cost = reserve_action_cost(
                self.session,
                budget=budget,
                context=budget_context,
                action_type="mcp_tool_call",
                amount_minor_units=amount,
                currency=currency,
                pricing_evidence={
                    "tool": tool_name,
                    "chargeable": chargeable,
                    "declared_pricing": pricing,
                    "configuration_snapshot_id": str(self.resolved.snapshot_id),
                },
            )
        except BudgetExhaustedError as error:
            self._record_rejection(tool_name, "budget_exhausted", reason=str(error))
            raise ToolBridgeError(str(error)) from error

        record_observability(
            self.session,
            self.context,
            event_kind="budget",
            operation_name="budget.cost_reserved",
            status=cost.ledger_entry.status,
            cost_ledger_entry_id=cost.ledger_entry.id,
            attributes={
                "action_type": "mcp_tool_call",
                "tool": tool_name,
                "amount_minor_units": amount,
                "currency": currency,
            },
        )
        self.session.commit()
        if self.control_check is not None:
            try:
                self.control_check()
            except Exception:
                release_action_cost(
                    self.session,
                    cost,
                    reason="goal control interrupted execution before tool side effect",
                )
                raise

        tool_call_id = uuid.uuid4()
        mcp_call_id = uuid.uuid4() if mcp_server_id is not None else None
        try:
            result = invoke_tool(tool_name, arguments)
        except TimeoutError as error:
            mark_action_cost_uncertain(self.session, cost, reason=str(error))
            self._record_observability_failure(
                tool_name, cost.ledger_entry.id, tool_call_id, mcp_call_id, error
            )
            raise ToolBridgeError(
                f"tool {tool_name!r} timed out with an uncertain external result"
            ) from error
        except Exception as error:
            release_action_cost(self.session, cost, reason=str(error))
            self._record_observability_failure(
                tool_name, cost.ledger_entry.id, tool_call_id, mcp_call_id, error
            )
            raise

        reconcile_action_cost(
            self.session,
            cost,
            actual_amount_minor_units=amount or 0,
            evidence={"tool": tool_name},
        )
        bounded, truncated, original_bytes = _bounded_result(result, output_limit)
        audit = AuditEvent(
            project_id=self.project.id,
            goal_id=self.goal.id,
            task_id=self.task.id,
            run_id=self.run.id,
            event_type="tool.invoked",
            payload={
                "tool": tool_name,
                "arguments": _redact(arguments),
                "result": bounded,
                "output_truncated": truncated,
                "configuration_snapshot_id": str(self.resolved.snapshot_id),
            },
        )
        self.session.add(audit)
        self.session.flush()
        for event_kind, operation_name in (
            ("tool_call", "tool.invoked"),
            *((("mcp_call", "mcp.tool.invoked"),) if mcp_call_id else ()),
        ):
            record_observability(
                self.session,
                self.context,
                event_kind=event_kind,
                operation_name=operation_name,
                status="completed",
                audit_event_id=audit.id,
                cost_ledger_entry_id=cost.ledger_entry.id,
                tool_call_id=tool_call_id,
                mcp_call_id=mcp_call_id,
                attributes={
                    "tool": tool_name,
                    "output_truncated": truncated,
                    "original_output_bytes": original_bytes,
                },
            )
        if truncated:
            self.session.add(
                AuditEvent(
                    project_id=self.project.id,
                    goal_id=self.goal.id,
                    task_id=self.task.id,
                    run_id=self.run.id,
                    event_type="tool.output_truncated",
                    payload={
                        "tool": tool_name,
                        "original_bytes": original_bytes,
                        "output_limit_bytes": output_limit,
                    },
                )
            )
        self.session.flush()
        return bounded

    def _has_approved_request(self, tool_name: str) -> bool:
        requests = self.session.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.task_id == self.task.id,
                ApprovalRequest.status == "approved",
            )
        ).scalars()
        for request in requests:
            preview = request.action_preview or {}
            if request.action_type == "run.execution":
                return True
            if (
                request.action_type in {"mcp.call", "tool.call"}
                and preview.get("tool") == tool_name
            ):
                return True
        return False

    def _record_rejection(
        self,
        tool_name: str,
        reason_code: str,
        *,
        reason: str | None = None,
        evaluations: list[dict[str, Any]] | None = None,
    ) -> None:
        self.session.add(
            AuditEvent(
                project_id=self.project.id,
                goal_id=self.goal.id,
                task_id=self.task.id,
                run_id=self.run.id,
                event_type="tool.rejected",
                payload={
                    "tool": tool_name,
                    "reason_code": reason_code,
                    "reason": reason,
                    "policy_evaluations": evaluations or [],
                    "configuration_snapshot_id": str(self.resolved.snapshot_id),
                },
            )
        )
        self.session.flush()
        record_observability(
            self.session,
            self.context,
            event_kind="tool_call",
            operation_name="tool.rejected",
            status="failed",
            attributes={"tool": tool_name, "reason_code": reason_code},
        )

    def _record_observability_failure(
        self,
        tool_name: str,
        cost_ledger_entry_id: uuid.UUID,
        tool_call_id: uuid.UUID,
        mcp_call_id: uuid.UUID | None,
        error: Exception,
    ) -> None:
        record_observability(
            self.session,
            self.context,
            event_kind="tool_call",
            operation_name="tool.invoked",
            status="failed",
            cost_ledger_entry_id=cost_ledger_entry_id,
            tool_call_id=tool_call_id,
            mcp_call_id=mcp_call_id,
            attributes={"tool": tool_name, "error_type": type(error).__name__},
        )
