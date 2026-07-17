from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.domain.models import ApprovalRequest, AuditEvent, Goal, Project, Run, Task
from agentic_os.worker.configuration import ResolvedRunConfiguration


def planned_governed_actions(
    task: Task,
    resolved: ResolvedRunConfiguration,
) -> list[dict[str, Any]]:
    """Describe every side-effect boundary before any of them is dispatched."""
    configuration = resolved.configuration
    actions: list[dict[str, Any]] = []
    if configuration.get("model_profile") is not None:
        actions.append({"action_type": "model.call", "model_profile_version_id": configuration["model_profile"]["id"]})
    for skill in configuration["skills"]:
        actions.append({"action_type": "skill.access", "skill_version_id": skill["id"]})
    for tool_name in resolved.enabled_tools:
        descriptor = resolved.tool_descriptor(tool_name)
        owner_id = next(
            (
                server["id"]
                for server in configuration["mcp_servers"]
                if descriptor in server["connection_config"].get("tools", [])
            ),
            None,
        )
        action = {
            "action_type": "mcp.call" if owner_id else "tool.call",
            "tool": tool_name,
        }
        if owner_id:
            action["mcp_server_version_id"] = owner_id
        actions.append(action)
    sandbox = configuration["agent"]["capability_manifest"].get("sandbox")
    if sandbox:
        actions.append({"action_type": "sandbox.lifecycle", "image": sandbox.get("image", "alpine:latest")})
        if sandbox.get("network_policy", "none") != "none":
            actions.append(
                {"action_type": "network.permission", "network_policy": sandbox["network_policy"]}
            )
    for intent in task.resource_intent or []:
        actions.append(
            {
                "action_type": "resource.permission",
                "resource_key": intent.get("resource_key"),
                "intent": intent.get("intent", "write"),
            }
        )
    actions.extend(
        [
            {"action_type": "workspace.promotion", "task_id": str(task.id)},
            {"action_type": "artifact.promotion", "kind": "output"},
        ]
    )
    return actions


def ensure_action_approvals(
    session: Session,
    *,
    project: Project,
    goal: Goal,
    task: Task,
    run: Run,
    resolved: ResolvedRunConfiguration,
) -> tuple[str, list[ApprovalRequest]]:
    """Create/reuse approval requests and return approved, pending, or rejected.

    All required requests are materialized before execution reaches its first
    side effect. Retries match the stable action key stored in the preview,
    preserving the original request identity after a restart or approval.
    """
    approval = resolved.approval_configuration
    mode = approval["mode"]
    consequential = set(approval.get("consequential_action_types", []))
    required = []
    for index, action in enumerate(planned_governed_actions(task, resolved)):
        if mode == "consequential" and action["action_type"] in consequential:
            required.append((index, action))
        elif mode == "every_tool_call" and action["action_type"] in {"mcp.call", "tool.call"}:
            required.append((index, action))

    if resolved.policy_decision == "approval_required":
        required.insert(0, (-1, {"action_type": "run.execution", "reason": "policy_decision"}))
    if not required:
        return "approved", []

    existing = list(
        session.execute(
            select(ApprovalRequest)
            .where(ApprovalRequest.task_id == task.id)
            .order_by(ApprovalRequest.created_at)
        ).scalars()
    )
    by_key = {
        request.action_preview.get("action_key"): request
        for request in existing
        if request.action_preview.get("action_key")
    }
    context = approval.get("context", {})
    ttl_seconds = max(int(context.get("approval_ttl_seconds", 3600)), 1)
    policy_ids = [item["id"] for item in resolved.configuration.get("policy_sets", [])]
    policy_ids.extend(item["id"] for item in resolved.configuration.get("task_policies", []))
    requests: list[ApprovalRequest] = []
    for index, action in required:
        action_key = f"{resolved.snapshot_id}:{index}:{action['action_type']}"
        request = by_key.get(action_key)
        if request is None:
            request = ApprovalRequest(
                team_id=project.team_id,
                project_id=project.id,
                goal_id=goal.id,
                task_id=task.id,
                run_id=run.id,
                agent_version_id=run.agent_version_id,
                configuration_id=(uuid.UUID(approval["id"]) if approval.get("id") else None),
                requested_by=goal.created_by,
                mode=mode,
                status="pending",
                action_type=action["action_type"],
                action_preview={"action_key": action_key, **action},
                policy_version_ids=policy_ids,
                policy_evidence={
                    "decision": resolved.policy_decision,
                    "evaluations": resolved.policy_evaluations,
                    "configuration_snapshot_id": str(resolved.snapshot_id),
                },
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
            )
            session.add(request)
            session.flush()
            session.add(
                AuditEvent(
                    project_id=project.id,
                    goal_id=goal.id,
                    task_id=task.id,
                    run_id=run.id,
                    event_type="approval.requested",
                    payload={
                        "approval_request_id": str(request.id),
                        "action_type": request.action_type,
                        "action_preview": request.action_preview,
                        "expires_at": request.expires_at.isoformat(),
                    },
                )
            )
        requests.append(request)
    session.flush()

    if any(request.status in {"denied", "expired", "cancelled"} for request in requests):
        return "rejected", requests
    if any(request.status == "pending" for request in requests):
        return "pending", requests
    return "approved", requests
