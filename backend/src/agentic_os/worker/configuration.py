from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from agentic_os.domain.models import (
    Agent,
    AgentVersion,
    AgentVersionMcpServer,
    AgentVersionPolicySet,
    AgentVersionSkill,
    ApprovalModeConfiguration,
    Budget,
    Credential,
    McpServer,
    McpServerAttachment,
    McpServerHealthCheck,
    McpServerTool,
    McpServerVersion,
    ModelProfileVersion,
    PlanningAssignment,
    PlanningCandidate,
    Policy,
    PolicySetVersion,
    Project,
    Run,
    RunConfigurationSnapshot,
    SkillVersion,
    Task,
)
from agentic_os.worker.governance import BudgetLimit, combine_decisions, evaluate_action_policy
from agentic_os.worker.tools import BUILTIN_TOOL_DESCRIPTORS


class ConfigurationSnapshotError(RuntimeError):
    """Raised when a worker cannot safely resolve its pinned configuration."""

    reason_code = "credential_or_visibility_revoked"


class McpToolUnavailableError(ConfigurationSnapshotError):
    """Raised when a granted MCP tool is no longer discovered, enabled, or schema-valid."""

    reason_code = "tool_disabled"


class McpHealthDegradedError(ConfigurationSnapshotError):
    """Raised when a granted MCP server has no healthy health-check evidence on record."""

    reason_code = "mcp_health_degraded"


class PlanningAssignmentInvalidError(ConfigurationSnapshotError):
    """Raised when the task's originating planning assignment is no longer valid."""

    reason_code = "planning_assignment_invalidated"


class PlanningAssignmentDriftError(ConfigurationSnapshotError):
    """Raised when the live planning assignment no longer matches the pinned task."""

    reason_code = "planning_assignment_drifted"


@dataclass(frozen=True)
class ResolvedRunConfiguration:
    snapshot_id: uuid.UUID
    configuration: dict[str, Any]

    @property
    def enabled_tools(self) -> list[str]:
        return list(self.configuration["enabled_tools"])

    @property
    def policy_decision(self) -> str:
        return str(self.configuration["policy_decision"])

    @property
    def policy_evaluations(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.configuration["policy_evaluations"])

    @property
    def approval_configuration(self) -> dict[str, Any]:
        return copy.deepcopy(
            self.configuration.get(
                "approval_configuration",
                {
                    "id": None,
                    "mode": "auto",
                    "consequential_action_types": [],
                    "context": {},
                },
            )
        )

    @property
    def budget(self) -> BudgetLimit | None:
        value = self.configuration.get("budget")
        if value is None:
            return None
        return BudgetLimit(
            id=uuid.UUID(value["id"]),
            currency=value["currency"],
            amount_minor_units=int(value["amount_minor_units"]),
            enforcement_mode=value["enforcement_mode"],
            warning_threshold_percent=(
                int(value["warning_threshold_percent"])
                if value.get("warning_threshold_percent") is not None
                else None
            ),
        )

    def tool_descriptor(self, tool_name: str) -> dict[str, Any]:
        for server in self.configuration["mcp_servers"]:
            for descriptor in server["connection_config"].get("tools", []):
                if descriptor.get("name") == tool_name:
                    return copy.deepcopy(descriptor)
        if tool_name in self.enabled_tools and tool_name in BUILTIN_TOOL_DESCRIPTORS:
            return copy.deepcopy(BUILTIN_TOOL_DESCRIPTORS[tool_name])
        raise ConfigurationSnapshotError(f"tool {tool_name!r} is not configured in snapshot {self.snapshot_id}")

    def validate_tool_access(
        self, session: Session, *, tool_name: str, project: Project
    ) -> None:
        """Re-check mutable MCP access immediately before a tool side effect."""

        agent_id = uuid.UUID(self.configuration["agent"]["id"])
        for configured in self.configuration["mcp_servers"]:
            if not any(
                descriptor.get("name") == tool_name
                for descriptor in configured["connection_config"].get("tools", [])
            ):
                continue
            server = session.get(McpServer, uuid.UUID(configured["mcp_server_id"]))
            if server is None or not (
                server.team_id == project.team_id
                or server.project_id == project.id
                or (server.project_id is None and server.visibility in ("team", "public"))
            ):
                raise ConfigurationSnapshotError(
                    f"MCP definition access for tool {tool_name!r} is no longer available"
                )
            if configured.get("grant_type") == "mcp_tools":
                mcp_server_version_id = uuid.UUID(configured["id"])
                live_tool = session.execute(
                    select(McpServerTool).where(
                        McpServerTool.mcp_server_version_id == mcp_server_version_id,
                        McpServerTool.tool_name == tool_name,
                    )
                ).scalar_one_or_none()
                if live_tool is None or not live_tool.enabled or not live_tool.schema_valid:
                    raise McpToolUnavailableError(
                        f"MCP tool {tool_name!r} is no longer enabled with a valid schema"
                    )
                latest_health = session.execute(
                    select(McpServerHealthCheck)
                    .where(McpServerHealthCheck.mcp_server_version_id == mcp_server_version_id)
                    .order_by(McpServerHealthCheck.created_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if latest_health is None or latest_health.status != "healthy":
                    raise McpHealthDegradedError(
                        f"MCP server for tool {tool_name!r} does not have current healthy status evidence"
                    )
            if configured["connection_config"].get("credential_required") is not True:
                return
            raw_grant_id = configured.get("credential_grant_id")
            grant = (
                session.get(McpServerAttachment, uuid.UUID(raw_grant_id))
                if raw_grant_id
                else None
            )
            if (
                grant is None
                or grant.revoked_at is not None
                or grant.mcp_server_version_id != uuid.UUID(configured["id"])
                or not (
                    grant.agent_id == agent_id
                    or grant.project_id == project.id
                    or grant.team_id == project.team_id
                )
            ):
                raise ConfigurationSnapshotError(
                    f"MCP credential access for tool {tool_name!r} is no longer available"
                )
            credential = session.get(Credential, grant.credential_id) if grant.credential_id else None
            if credential is None or not (
                credential.team_id == project.team_id or credential.project_id == project.id
            ):
                raise ConfigurationSnapshotError(
                    f"MCP credential access for tool {tool_name!r} is outside the run scope"
                )
            return
        if tool_name in self.enabled_tools and tool_name in BUILTIN_TOOL_DESCRIPTORS:
            return
        raise ConfigurationSnapshotError(
            f"tool {tool_name!r} is not configured in snapshot {self.snapshot_id}"
        )


def _validate_planning_assignment(session: Session, task: Task) -> None:
    """Re-check that an accepted plan's live assignment still matches the task.

    A planning assignment can be overridden after its planning session was
    accepted (overrides are not blocked by acceptance status). That leaves a
    window where the task this run is executing was pinned to one agent
    version while the plan's live assignment record now points elsewhere, or
    has been invalidated outright. This runs on every attempt, including
    retries that reuse a cached snapshot, since it validates mutable
    planning-authority state rather than the immutable snapshot payload.
    """
    if task.planning_assignment_id is None:
        return
    assignment = session.get(PlanningAssignment, task.planning_assignment_id)
    if assignment is None:
        raise PlanningAssignmentInvalidError(
            f"planning assignment {task.planning_assignment_id} for task {task.id} no longer exists"
        )
    if assignment.validation_status != "valid":
        raise PlanningAssignmentInvalidError(
            f"planning assignment {assignment.id} for task {task.id} is no longer valid "
            f"(validation_status={assignment.validation_status!r})"
        )
    candidate = (
        session.get(PlanningCandidate, assignment.candidate_id)
        if assignment.candidate_id
        else None
    )
    if candidate is None or candidate.agent_version_id != task.assigned_agent_version_id:
        raise PlanningAssignmentDriftError(
            f"planning assignment {assignment.id} for task {task.id} no longer selects the "
            f"agent version this task was materialized with"
        )


def resolve_run_configuration(
    session: Session,
    *,
    task: Task,
    run: Run,
    project: Project,
) -> ResolvedRunConfiguration:
    """Create the task's first immutable snapshot or reuse it for a retry.

    ``RunConfigurationSnapshot.run_id`` records the attempt that established
    the configuration. Later attempts retain the same snapshot identity in
    ``Run.snapshot`` and execute exclusively from its copied JSON payload.
    """
    _validate_planning_assignment(session, task)
    previous_snapshot = session.execute(
        select(RunConfigurationSnapshot)
        .join(Run, RunConfigurationSnapshot.run_id == Run.id)
        .where(Run.task_id == task.id, Run.id != run.id)
        .order_by(Run.attempt_number)
        .limit(1)
    ).scalar_one_or_none()
    if previous_snapshot is not None:
        configuration = copy.deepcopy(previous_snapshot.configuration)
        _validate_snapshot(configuration, previous_snapshot.id)
        return ResolvedRunConfiguration(previous_snapshot.id, configuration)

    agent_version = session.get(AgentVersion, task.assigned_agent_version_id)
    if agent_version is None:
        raise ConfigurationSnapshotError(
            f"assigned agent version {task.assigned_agent_version_id} not found"
        )
    agent = session.get(Agent, agent_version.agent_id)
    if agent is None:
        raise ConfigurationSnapshotError(f"agent {agent_version.agent_id} not found")
    if agent.team_id != project.team_id:
        raise ConfigurationSnapshotError("assigned agent and project belong to different teams")

    model_profile = None
    if agent_version.model_profile_version_id is not None:
        model_profile = session.get(ModelProfileVersion, agent_version.model_profile_version_id)
        if model_profile is None:
            raise ConfigurationSnapshotError(
                f"model profile version {agent_version.model_profile_version_id} not found"
            )

    skill_attachments = list(
        session.execute(
            select(AgentVersionSkill)
            .where(AgentVersionSkill.agent_version_id == agent_version.id)
            .order_by(AgentVersionSkill.created_at, AgentVersionSkill.id)
        ).scalars()
    )
    skills: list[dict[str, Any]] = []
    for attachment in skill_attachments:
        version = session.get(SkillVersion, attachment.skill_version_id)
        if version is None:
            raise ConfigurationSnapshotError(f"skill version {attachment.skill_version_id} not found")
        attachment_config = copy.deepcopy(attachment.attachment_config or {})
        skill_entry: dict[str, Any] = {
            "id": str(version.id),
            "skill_id": str(version.skill_id),
            "version_number": version.version_number,
            "content_ref": version.content_ref,
            "resource_metadata": copy.deepcopy(version.resource_metadata or {}),
            "attachment_config": attachment_config,
            "grant_type": attachment_config.get("grant_type"),
        }
        if attachment_config.get("grant_type") == "skill_resources":
            resource_paths = list(attachment_config.get("resource_paths", []))
            granted_resources = [
                copy.deepcopy(resource)
                for resource in (version.resources or [])
                if isinstance(resource, dict) and resource.get("path") in resource_paths
            ]
            missing_paths = sorted(
                set(resource_paths) - {resource.get("path") for resource in granted_resources}
            )
            if missing_paths:
                raise ConfigurationSnapshotError(
                    f"granted skill resources {missing_paths} are no longer present on "
                    f"skill version {version.id}"
                )
            skill_entry["resource_paths"] = resource_paths
            skill_entry["resources"] = granted_resources
            skill_entry["package_hash"] = version.package_hash
            skill_entry["declared_capabilities"] = copy.deepcopy(version.declared_capabilities or [])
            skill_entry["provenance"] = copy.deepcopy(version.provenance or {})
        skills.append(skill_entry)

    mcp_attachments = list(
        session.execute(
            select(AgentVersionMcpServer)
            .where(AgentVersionMcpServer.agent_version_id == agent_version.id)
            .order_by(AgentVersionMcpServer.created_at, AgentVersionMcpServer.id)
        ).scalars()
    )
    mcp_servers: list[dict[str, Any]] = []
    tool_owners: dict[str, str] = {}
    for attachment in mcp_attachments:
        version = session.get(McpServerVersion, attachment.mcp_server_version_id)
        if version is None:
            raise ConfigurationSnapshotError(
                f"MCP server version {attachment.mcp_server_version_id} not found"
            )
        server = session.get(McpServer, version.mcp_server_id)
        if server is None:
            raise ConfigurationSnapshotError(f"MCP server {version.mcp_server_id} not found")
        definition_accessible = (
            server.team_id == project.team_id
            or server.project_id == project.id
            or (server.project_id is None and server.visibility in ("team", "public"))
        )
        if not definition_accessible:
            raise ConfigurationSnapshotError(
                f"MCP definition {server.id} is not accessible to project {project.id}"
            )
        connection_config = copy.deepcopy(version.connection_config or {})
        attachment_config = copy.deepcopy(attachment.attachment_config or {})
        grant_type = attachment_config.get("grant_type")
        if grant_type == "mcp_tools":
            pinned_tools = attachment_config.get("tools", [])
            if not pinned_tools:
                raise ConfigurationSnapshotError(
                    f"MCP tool grant on version {version.id} has no selected tools"
                )
            selected_names = [item.get("name") for item in pinned_tools]
            live_tools = {
                tool.tool_name: tool
                for tool in session.execute(
                    select(McpServerTool).where(
                        McpServerTool.mcp_server_version_id == version.id,
                        McpServerTool.tool_name.in_(selected_names),
                    )
                ).scalars()
            }
            resolved_tools: list[dict[str, Any]] = []
            for pinned in pinned_tools:
                name = pinned.get("name")
                live = live_tools.get(name)
                if live is None:
                    raise ConfigurationSnapshotError(
                        f"granted MCP tool {name!r} is no longer discovered on version {version.id}"
                    )
                tool_entry: dict[str, Any] = {
                    "name": name,
                    "description": live.description or "",
                    "input_schema": copy.deepcopy(live.input_schema or {}),
                }
                if pinned.get("timeout_ms") is not None:
                    tool_entry["timeout_ms"] = pinned["timeout_ms"]
                if pinned.get("output_limit_bytes") is not None:
                    tool_entry["output_limit_bytes"] = pinned["output_limit_bytes"]
                resolved_tools.append(
                    {
                        **tool_entry,
                        "pinned_descriptor_hash": pinned.get("descriptor_hash"),
                        "credential_scope_required": live.credential_scope_required,
                    }
                )
            connection_config["tools"] = [
                {key: value for key, value in item.items() if key not in ("pinned_descriptor_hash", "credential_scope_required")}
                for item in resolved_tools
            ]
            connection_config["credential_required"] = bool(
                connection_config.get("credential_required")
            ) or any(item["credential_scope_required"] for item in resolved_tools)
        grants = list(
            session.execute(
                select(McpServerAttachment).where(
                    McpServerAttachment.mcp_server_version_id == version.id,
                    McpServerAttachment.revoked_at.is_(None),
                    or_(
                        McpServerAttachment.agent_id == agent.id,
                        McpServerAttachment.project_id == project.id,
                        McpServerAttachment.team_id == project.team_id,
                    ),
                )
            ).scalars()
        )
        grants.sort(
            key=lambda item: (
                0 if item.agent_id == agent.id else 1 if item.project_id == project.id else 2,
                item.created_at,
            )
        )
        grant = grants[0] if grants else None
        credential = session.get(Credential, grant.credential_id) if grant and grant.credential_id else None
        if credential is not None and not (
            credential.team_id == project.team_id or credential.project_id == project.id
        ):
            raise ConfigurationSnapshotError(
                f"MCP credential grant {grant.id} is outside the run scope"
            )
        if connection_config.get("credential_required") is True and credential is None:
            raise ConfigurationSnapshotError(
                f"MCP server version {version.id} requires an active scoped credential grant"
            )
        for descriptor in connection_config.get("tools", []):
            name = descriptor.get("name")
            if not isinstance(name, str) or not name:
                raise ConfigurationSnapshotError(
                    f"MCP server version {version.id} contains a tool without a valid name"
                )
            if name in tool_owners:
                raise ConfigurationSnapshotError(
                    f"tool {name!r} is ambiguously configured by MCP versions "
                    f"{tool_owners[name]} and {version.id}"
                )
            tool_owners[name] = str(version.id)
        mcp_servers.append(
            {
                "id": str(version.id),
                "mcp_server_id": str(version.mcp_server_id),
                "version_number": version.version_number,
                "connection_config": connection_config,
                "credential_grant_id": str(grant.id) if grant else None,
                "credential_configured": credential is not None,
                "attachment_config": attachment_config,
                "grant_type": grant_type,
                "pinned_tool_descriptor_hashes": (
                    {item["name"]: item.get("descriptor_hash") for item in attachment_config.get("tools", [])}
                    if grant_type == "mcp_tools"
                    else {}
                ),
            }
        )

    manifest = copy.deepcopy(agent_version.capability_manifest or {})
    enabled_tools = manifest.get("enabled_tools", [])
    if not isinstance(enabled_tools, list) or not all(
        isinstance(name, str) and name for name in enabled_tools
    ):
        raise ConfigurationSnapshotError("capability_manifest.enabled_tools must be a list of names")
    missing_tools = sorted(
        set(enabled_tools) - set(tool_owners) - set(BUILTIN_TOOL_DESCRIPTORS)
    )
    if missing_tools:
        raise ConfigurationSnapshotError(
            f"unconfigured tools requested by agent version {agent_version.id}: {missing_tools}"
        )
    _validate_legacy_attachment_ref(manifest, "skill_version_id", {item["id"] for item in skills})
    _validate_legacy_attachment_ref(
        manifest, "mcp_server_version_id", {item["id"] for item in mcp_servers}
    )

    policy_sets: list[dict[str, Any]] = []
    for attachment in session.execute(
        select(AgentVersionPolicySet)
        .where(AgentVersionPolicySet.agent_version_id == agent_version.id)
        .order_by(AgentVersionPolicySet.created_at, AgentVersionPolicySet.id)
    ).scalars():
        version = session.get(PolicySetVersion, attachment.policy_set_version_id)
        if version is None:
            raise ConfigurationSnapshotError(
                f"policy set version {attachment.policy_set_version_id} not found"
            )
        policy_sets.append(
            {
                "id": str(version.id),
                "policy_set_id": str(version.policy_set_id),
                "version_number": version.version_number,
                "rules": copy.deepcopy(version.rules or []),
            }
        )

    task_policies: list[dict[str, Any]] = []
    for raw_policy_id in task.policy_ids or []:
        policy = session.get(Policy, uuid.UUID(str(raw_policy_id)))
        if policy is None:
            raise ConfigurationSnapshotError(f"task policy {raw_policy_id} not found")
        task_policies.append(
            {
                "id": str(policy.id),
                "scope_type": policy.scope_type,
                "scope_id": str(policy.scope_id) if policy.scope_id else None,
                "decision": policy.decision,
                "rule": copy.deepcopy(policy.rule or {}),
            }
        )

    evaluations: list[dict[str, Any]] = []
    decisions: list[str] = []
    mcp_ids = [uuid.UUID(item["mcp_server_id"]) for item in mcp_servers] or [None]
    for mcp_server_id in mcp_ids:
        decision, current = evaluate_action_policy(
            session, agent_id=agent.id, mcp_server_id=mcp_server_id
        )
        decisions.append(decision)
        for item in current:
            if item not in evaluations:
                evaluations.append(item)
    decisions.extend(item["decision"] for item in task_policies)
    policy_decision = combine_decisions(decisions)

    budget_id = task.budget_id or agent_version.default_budget_id
    budget = session.get(Budget, budget_id) if budget_id else None
    if budget_id is not None and budget is None:
        raise ConfigurationSnapshotError(f"budget {budget_id} not found")
    if budget is not None and budget.agent_id != agent.id:
        raise ConfigurationSnapshotError(f"budget {budget.id} belongs to another agent")

    approval_configuration = session.execute(
        select(ApprovalModeConfiguration)
        .where(
            ApprovalModeConfiguration.project_id == project.id,
            ApprovalModeConfiguration.goal_id == task.goal_id,
        )
        .order_by(ApprovalModeConfiguration.version_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if approval_configuration is None:
        approval_configuration = session.execute(
            select(ApprovalModeConfiguration)
            .where(
                ApprovalModeConfiguration.project_id == project.id,
                ApprovalModeConfiguration.goal_id.is_(None),
            )
            .order_by(ApprovalModeConfiguration.version_number.desc())
            .limit(1)
        ).scalar_one_or_none()

    snapshot_id = uuid.uuid4()
    configuration: dict[str, Any] = {
        "schema_version": 1,
        "snapshot_id": str(snapshot_id),
        "agent": {
            "id": str(agent.id),
            "version_id": str(agent_version.id),
            "version_number": agent_version.version_number,
            "instructions": agent_version.instructions,
            "capability_manifest": manifest,
        },
        "model_profile": _model_profile_payload(model_profile),
        "skills": skills,
        "mcp_servers": mcp_servers,
        "policy_sets": policy_sets,
        "task_policies": task_policies,
        "policy_decision": policy_decision,
        "policy_evaluations": evaluations,
        "approval_configuration": _approval_configuration_payload(approval_configuration),
        "budget": _budget_payload(budget),
        "enabled_tools": list(enabled_tools),
        "assignment_rationale": copy.deepcopy(task.assignment_rationale or {}),
        "task": {
            "id": str(task.id),
            "required_capabilities": copy.deepcopy(task.required_capabilities or {}),
            "capability_rationale": copy.deepcopy(task.capability_rationale or {}),
        },
        "planning": {
            "planning_session_id": (
                str(task.planning_session_id) if task.planning_session_id else None
            ),
            "planning_assignment_id": (
                str(task.planning_assignment_id) if task.planning_assignment_id else None
            ),
        },
    }
    row = RunConfigurationSnapshot(
        id=snapshot_id,
        run_id=run.id,
        team_id=project.team_id,
        project_id=project.id,
        agent_version_id=agent_version.id,
        model_profile_version_id=agent_version.model_profile_version_id,
        budget_id=budget.id if budget else None,
        configuration=configuration,
    )
    session.add(row)
    session.flush()
    return ResolvedRunConfiguration(snapshot_id, copy.deepcopy(configuration))


def _validate_snapshot(configuration: dict[str, Any], snapshot_id: uuid.UUID) -> None:
    if configuration.get("snapshot_id") != str(snapshot_id):
        raise ConfigurationSnapshotError(f"configuration snapshot {snapshot_id} has inconsistent identity")
    for key in ("agent", "skills", "mcp_servers", "enabled_tools", "policy_decision"):
        if key not in configuration:
            raise ConfigurationSnapshotError(f"configuration snapshot {snapshot_id} is missing {key!r}")


def _validate_legacy_attachment_ref(manifest: dict, key: str, attached_ids: set[str]) -> None:
    raw_id = manifest.get(key)
    if raw_id is not None and str(raw_id) not in attached_ids:
        raise ConfigurationSnapshotError(f"{key} {raw_id} is not attached to the agent version")


def _model_profile_payload(version: ModelProfileVersion | None) -> dict[str, Any] | None:
    if version is None:
        return None
    return {
        "id": str(version.id),
        "model_profile_id": str(version.model_profile_id),
        "version_number": version.version_number,
        "base_url": version.base_url,
        "model_identifier": version.model_identifier,
        "credential_id": str(version.credential_id) if version.credential_id else None,
        "headers": copy.deepcopy(version.headers or {}),
        "capability_metadata": copy.deepcopy(version.capability_metadata or {}),
        "pricing_metadata": copy.deepcopy(version.pricing_metadata or {}),
    }


def _budget_payload(budget: Budget | None) -> dict[str, Any] | None:
    if budget is None:
        return None
    return {
        "id": str(budget.id),
        "currency": budget.currency,
        "amount_minor_units": budget.amount_minor_units,
        "enforcement_mode": budget.enforcement_mode,
        "warning_threshold_percent": budget.warning_threshold_percent,
    }


def _approval_configuration_payload(
    configuration: ApprovalModeConfiguration | None,
) -> dict[str, Any]:
    if configuration is None:
        return {
            "id": None,
            "mode": "auto",
            "consequential_action_types": [],
            "context": {},
        }
    return {
        "id": str(configuration.id),
        "mode": configuration.mode,
        "consequential_action_types": list(configuration.consequential_action_types or []),
        "context": copy.deepcopy(configuration.context or {}),
    }
